import logging
import requests
import base64
import time
from datetime import datetime
from dateutil import parser
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class ShopifySync(models.Model):
    _name = "shopify.sync"
    _description = "Sync Shopify Products to Odoo"

    name = fields.Char("Sync Name", required=True)
    shopify_id = fields.Char("Shopify ID")
    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], string="Status", default="pending")
    sync_date = fields.Datetime("Last Sync")

    @api.model
    def _auto_sync_products(self):
        """Auto-sync Shopify products (for cron jobs)"""
        _logger.info("Running auto Shopify sync...")
        return self.sync_products_to_odoo()

    @api.model
    def fetch_shopify_products(self, last_sync_time=None, batch_size=50):
        """
        Fetch Shopify products with pagination, sorted by `updated_at` ascending
        
        Args:
            last_sync_time: Only fetch products updated since this time
            batch_size: Number of products to fetch per batch (default: 50)
        """
        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')
        # last_page_info = self.env['ir.config_parameter'].sudo().get_param('shopify.last_page_info', False)

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials. Ensure they are set in system parameters.")
            return []

        base_url = f"https://{shopify_store_url}/admin/api/2023-01/products.json"
        headers = {"X-Shopify-Access-Token": shopify_access_token}

        # Start a new sync sequence
        params = {
            "limit": batch_size,
            "order": "updated_at asc"  # Get oldest updates first to ensure we don't miss anything
        }
        
        if last_sync_time:
            # Format timestamp properly for Shopify API
            _logger.info(f"Filtering products updated since: {last_sync_time}")
            params["updated_at_min"] = last_sync_time
        
        all_products = []
        
        try:
            _logger.info(f"Fetching Shopify products batch (max {batch_size})")
            
            # Remove any angle brackets that might wrap the URL
            if base_url.startswith('<') and base_url.endswith('>'):
                base_url = base_url[1:-1]
            
            response = requests.get(base_url, headers=headers, params=params)
            
            if response.status_code == 429:  # Rate limit hit
                retry_after = int(response.headers.get('Retry-After', 10))
                _logger.warning(f"Rate limit hit, waiting {retry_after} seconds")
                time.sleep(retry_after)
                return self.fetch_shopify_products(last_sync_time, batch_size)  # Retry after waiting
                
            elif response.status_code != 200:
                _logger.error(f"Shopify API Error: {response.status_code} - {response.text}")
                return []
                
            data = response.json()
            all_products = data.get("products", [])
            
            _logger.info(f"Retrieved {len(all_products)} products in this batch")
            
            # Check for pagination (next page) and save it for the next cron run
            link_header = response.headers.get("Link", "")
            
            # Parse Link header properly to extract next URL page_info
            if link_header and 'rel="next"' in link_header:
                for link in link_header.split(','):
                    if 'rel="next"' in link:
                        # Extract URL from between < and >
                        url_part = link.split(';')[0].strip()
                        if url_part.startswith('<') and url_part.endswith('>'):
                            next_url = url_part[1:-1]  # Remove < and >
                            
                            # Extract the page_info parameter from the next URL
                            if 'page_info=' in next_url:
                                next_page_info = next_url.split('page_info=')[1].split('&')[0]
                                _logger.info(f"Extracted page_info token for next batch")
                                break
            
        except Exception as e:
            _logger.exception(f"Error fetching products from Shopify: {str(e)}")

        _logger.info(f"Fetched a total of {len(all_products)} products from Shopify.")
        return all_products

    @api.model
    def sync_products_to_odoo(self, batch_size=50):
        """
        Sync Shopify products & inventory to Odoo
        
        Args:
            batch_size: Number of products to sync per batch (default: 50)
        """
        _logger.info(f"Starting Shopify product sync (batch size: {batch_size})...")
        
        # Get timestamp from previous sync (for the very first sync)
        last_sync_time = self.env['ir.config_parameter'].sudo().get_param('shopify.last_sync_time')
        
        # Track the current time before syncing starts (will be used if we need to restart pagination)
        current_sync_time = datetime.now().isoformat()
        
        # Fetch one batch of products
        shopify_products = self.fetch_shopify_products(last_sync_time, batch_size)

        if len(shopify_products) > 1:
            self.env['ir.config_parameter'].sudo().set_param('shopify.last_sync_time', current_sync_time)

        ProductModel = self.env['product.product']
        InventoryModel = self.env['stock.quant']
        updated, created, skipped = 0, 0, 0

        for product in shopify_products:
            # Basic validation
            # if not product.get("variants") or not product["variants"]:
            #     _logger.warning(f"Skipping product ID {product.get('id')} - no variants found")
            #     skipped += 1
            #     continue
                
            shopify_product_id = str(product["id"])  # Convert to string for consistent comparison
            sku = product["variants"][0].get("sku", "")
            quantity = product["variants"][0].get("inventory_quantity", 0)
            price = float(product["variants"][0].get("price", 0.0))

            if not sku:
                _logger.info(f"Product '{product['title']}' (ID: {shopify_product_id}) has no SKU.")
                # _logger.warning(f"Skipping product '{product['title']}' (ID: {shopify_product_id}) due to missing SKU.")
                # skipped += 1
                # continue

            # First try to find by Shopify product ID (more reliable than SKU)
            existing_product = ProductModel.search([('shopify_product_id', '=', shopify_product_id)], limit=1)
            
            # If not found by Shopify ID, try to find by SKU
            if not existing_product:
                existing_product = ProductModel.search([('default_code', '=', sku)], limit=1)

            product_data = {
                'name': product['title'],
                'default_code': sku if sku else None,
                'list_price': price,
                'description_sale': product.get("body_html", ""),
                'shopify_product_id': shopify_product_id,
                'type': 'product'
            }

            # Handle product image
            image_url = (product.get("image", {}) or {}).get("src")
            if image_url:
                try:
                    image_response = requests.get(image_url, timeout=10)
                    if image_response.status_code == 200:
                        product_data['image_1920'] = base64.b64encode(image_response.content).decode('utf-8')
                        _logger.info(f"Image fetched for product: {product['title']}")
                    else:
                        _logger.warning(f"Failed to fetch image for {product['title']}: Status code {image_response.status_code}")
                except Exception as e:
                    _logger.warning(f"Failed to fetch image for {product['title']}: {str(e)}")

            # Update or create product
            if existing_product:
                existing_product.write(product_data)
                updated += 1
                _logger.info(f"Updated product: {product['title']} (SKU: {sku}, ID: {shopify_product_id})")
            else:
                existing_product = ProductModel.create(product_data)
                created += 1
                _logger.info(f"Created new product: {product['title']} (SKU: {sku}, ID: {shopify_product_id})")

            # Update inventory
            try:
                valid_location = self.env['stock.location'].search([
                    ('usage', '=', 'internal'),  # Only use internal locations
                    ('company_id', '=', self.env.company.id)
                ], limit=1)
                
                if not valid_location:
                    _logger.error("No valid storage location found for inventory sync.")
                    continue

                inventory_data = {
                    'product_id': existing_product.id,
                    'location_id': valid_location.id,
                    'quantity': quantity,
                }

                existing_inventory = InventoryModel.search([
                    ('product_id', '=', existing_product.id),
                    ('location_id', '=', valid_location.id)
                ], limit=1)

                if existing_inventory:
                    existing_inventory.sudo().write({'quantity': quantity})
                    _logger.info(f"Updated inventory for {product['title']} to {quantity}")
                else:
                    InventoryModel.sudo().create(inventory_data)
                    _logger.info(f"Created inventory entry for {product['title']} with quantity {quantity}")
                    
            except Exception as e:
                _logger.exception(f"Error updating inventory for product {product['title']}: {str(e)}")

        self.env['ir.config_parameter'].sudo().set_param('shopify.last_sync_time', current_sync_time)

        _logger.info(f"Batch sync completed: {created} created, {updated} updated, {skipped} skipped.")
        return {
            "status": "success", 
            "updated": updated, 
            "created": created,
            "skipped": skipped
        }


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_product_id = fields.Char("Shopify Product ID", index=True, help="ID of the product in Shopify")
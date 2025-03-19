import logging
import requests
from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)  # Initialize logger

class ShopifySync(models.Model):
    _name = "shopify.sync"
    _description = "Sync Shopify Products to Odoo"

    @api.model
    def _auto_sync_products(self):
        """Auto-sync Shopify products (for cron jobs)"""
        _logger.info("Running auto Shopify sync...")
        self.sync_products_to_odoo()

    def fetch_shopify_products(self):
        """Fetch first 5 products from Shopify API for testing"""
        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials. Ensure they are set in system parameters.")
            return []

        url = f"https://{shopify_store_url}/admin/api/2023-01/products.json?limit=5"
        headers = {"X-Shopify-Access-Token": shopify_access_token}
        
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                products = response.json().get("products", [])[:5]
                _logger.info(f"Fetched {len(products)} products from Shopify.")  # Log success
                return products
            else:
                _logger.error(f"Shopify API Error: {response.status_code} - {response.text}")  # Log error
                return []
        except Exception as e:
            _logger.exception(f"Error fetching products from Shopify: {str(e)}")  # Log exceptions
            return []

    @api.model
    def sync_products_to_odoo(self):
        """Sync Shopify products & inventory to Odoo"""
        _logger.info("Starting Shopify product sync...")
        shopify_products = self.fetch_shopify_products()
        
        if not shopify_products:
            _logger.warning("No products fetched from Shopify.")
            return {"status": "warning", "message": "No products fetched."}

        ProductModel = self.env['product.product']
        InventoryModel = self.env['stock.quant']
        updated, created = 0, 0

        for product in shopify_products:
            sku = product["variants"][0].get("sku", "")
            quantity = product["variants"][0].get("inventory_quantity", 0)
            price = float(product["variants"][0]["price"])
            shopify_product_id = product["id"]

            if not sku:
                _logger.warning(f"Skipping product '{product['title']}' due to missing SKU.")
                continue

            existing_product = ProductModel.search([('default_code', '=', sku)], limit=1)

            product_data = {
                'name': product['title'],
                'default_code': sku,
                'list_price': price,
                'description_sale': product.get("body_html", ""),
                'shopify_product_id': shopify_product_id,
				'type': 'product'
            }

            if existing_product:
                existing_product.write(product_data)
                updated += 1
                _logger.info(f"Updated product: {product['title']} (SKU: {sku})")
            else:
                existing_product = ProductModel.create(product_data)
                created += 1
                _logger.info(f"Created new product: {product['title']} (SKU: {sku})")

            # Find a valid storage location (non-view type)
            valid_location = self.env['stock.location'].search([('usage', '!=', 'view')], limit=1)
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
                existing_inventory.sudo().write({'inventory_quantity': quantity})
            else:
                InventoryModel.sudo().create(inventory_data)

        _logger.info(f"Sync completed: {created} created, {updated} updated.")
        return {"status": "success", "updated": updated, "created": created}


# Extend product.product to add shopify_product_id field
class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_product_id = fields.Char("Shopify Product ID", index=True, help="ID of the product in Shopify")

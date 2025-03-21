# File: odoo_sync.py
import logging
import requests
import json
import base64
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class OdooSync(models.Model):
	_name = "odoo.sync"
	_description = "Sync Odoo Products to Shopify"

	@api.model
	def _auto_sync_to_shopify(self):
		"""Auto-sync Odoo products to Shopify (for cron jobs)"""
		_logger.info("Running auto Odoo to Shopify sync...")
		return self.sync_products_to_shopify()
		
	@api.model
    def test_connection(self):
        # Add your Shopify connection test logic here
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': 'Shopify connection successful!',
                'sticky': False,
            }
        }

	@api.model
	def sync_products_to_shopify(self, product_ids=None):
		"""
		Sync Odoo products to Shopify
		
		Args:
			product_ids: Specific product IDs to sync, or None for modified products
		"""
		_logger.info("Starting Odoo to Shopify product sync...")
		
		shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
		shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')
		
		if not shopify_access_token or not shopify_store_url:
			_logger.error("Missing Shopify credentials. Ensure they are set in system parameters.")
			return {
				"status": "error",
				"message": "Missing Shopify credentials"
			}
			
		headers = {
			"X-Shopify-Access-Token": shopify_access_token,
			"Content-Type": "application/json"
		}
		
		# If product_ids is provided, sync those specific products
		# Otherwise, sync products modified since last sync
		ProductModel = self.env['product.product']
		
		if product_ids:
			products_to_sync = ProductModel.browse(product_ids)
			_logger.info(f"Syncing {len(products_to_sync)} specific products to Shopify")
		else:
			# Get timestamp from previous sync to Shopify
			last_sync_to_shopify = self.env['ir.config_parameter'].sudo().get_param('shopify.last_export_time')
			
			if last_sync_to_shopify:
				products_to_sync = ProductModel.search([
					('write_date', '>', last_sync_to_shopify),
					('active', '=', True)
				])
				_logger.info(f"Found {len(products_to_sync)} products modified since {last_sync_to_shopify}")
			else:
				# First-time sync, start with a reasonable batch
				products_to_sync = ProductModel.search([('active', '=', True)], limit=50)
				_logger.info(f"First-time sync, processing first {len(products_to_sync)} active products")
				
		created, updated, failed = 0, 0, 0
		
		for product in products_to_sync:
			try:
				# Prepare product data for Shopify API
				product_data = self._prepare_shopify_product_data(product)
				
				# Check if product already exists in Shopify
				if product.shopify_product_id:
					# Update existing product
					product_url = f"https://{shopify_store_url}/admin/api/2023-01/products/{product.shopify_product_id}.json"
					response = requests.put(
						product_url,
						headers=headers,
						data=json.dumps({"product": product_data})
					)
					
					if response.status_code in [200, 201]:
						updated += 1
						_logger.info(f"Updated product in Shopify: {product.name} (ID: {product.shopify_product_id})")
					else:
						failed += 1
						_logger.error(f"Failed to update product {product.name} in Shopify: {response.status_code} - {response.text}")
						
				else:
					# Create new product
					product_url = f"https://{shopify_store_url}/admin/api/2023-01/products.json"
					response = requests.post(
						product_url,
						headers=headers,
						data=json.dumps({"product": product_data})
					)
					
					if response.status_code in [200, 201]:
						shopify_product = response.json().get("product", {})
						shopify_product_id = str(shopify_product.get("id"))
						
						# Store Shopify ID in Odoo product
						product.write({"shopify_product_id": shopify_product_id})
						
						created += 1
						_logger.info(f"Created new product in Shopify: {product.name} (Shopify ID: {shopify_product_id})")
					else:
						failed += 1
						_logger.error(f"Failed to create product {product.name} in Shopify: {response.status_code} - {response.text}")
				
			except Exception as e:
				failed += 1
				_logger.exception(f"Error syncing product {product.name} to Shopify: {str(e)}")
				
		# Update last sync timestamp
		self.env['ir.config_parameter'].sudo().set_param('shopify.last_export_time', fields.Datetime.now())
		
		_logger.info(f"Sync to Shopify completed: {created} created, {updated} updated, {failed} failed.")
		return {
			"status": "success",
			"created": created,
			"updated": updated,
			"failed": failed
		}
	
	def _prepare_shopify_product_data(self, product):
		"""
		Prepare Odoo product data for Shopify API
		
		Args:
			product: Odoo product.product record
		
		Returns:
			dict: Formatted product data for Shopify
		"""
		# Convert image to base64 if present
		image_data = None
		if product.image_1920:
			image_data = product.image_1920.decode('utf-8') if isinstance(product.image_1920, bytes) else product.image_1920
		
		# Prepare variant data
		variant = {
			"price": str(product.list_price),
			"sku": product.default_code or "",
			"inventory_management": "shopify",
			"inventory_quantity": self._get_product_stock_quantity(product),
			"requires_shipping": True
		}
		
		# Prepare product data
		product_data = {
			"title": product.name,
			"body_html": product.description_sale or "",
			"vendor": product.company_id.name if product.company_id else "",
			"product_type": product.categ_id.name if product.categ_id else "",
			"published": True,
			"variants": [variant]
		}
		
		# Add image if available
		if image_data:
			product_data["images"] = [{
				"attachment": image_data
			}]
			
		return product_data
	
	def _get_product_stock_quantity(self, product):
		"""
		Get available stock quantity for a product
		
		Args:
			product: Odoo product.product record
		
		Returns:
			int: Available quantity
		"""
		# Get available stock quantity
		quants = self.env['stock.quant'].search([
			('product_id', '=', product.id),
			('location_id.usage', '=', 'internal')
		])
		
		return int(sum(quants.mapped('quantity')))

class ProductProductShopify(models.Model):
    _inherit = "product.product"
    
    def sync_to_shopify(self):
        """Sync this product to Shopify (can be called from UI button)"""
        self.ensure_one()
        return self.env['shopify.export'].sync_products_to_shopify(product_ids=[self.id])
    
    @api.model_create_multi
    def create(self, vals_list):
        """Override create to sync new products to Shopify"""
        new_products = super(ProductProductShopify, self).create(vals_list)
        # Queue these products for Shopify sync
        self.env['shopify.export'].with_delay(priority=20).sync_products_to_shopify(product_ids=new_products.ids)
        return new_products
    
    def write(self, vals):
        """Override write to sync updated products to Shopify"""
        result = super(ProductProductShopify, self).write(vals)
        # Only sync if relevant fields changed
        relevant_fields = ['name', 'default_code', 'list_price', 'description_sale', 'image_1920', 'active']
        if any(field in vals for field in relevant_fields):
            self.env['shopify.export'].with_delay(priority=20).sync_products_to_shopify(product_ids=self.ids)
        return result
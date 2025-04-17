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

	name = fields.Char("Sync Name", required=True)
	sync_status = fields.Selection([
		('pending', 'Pending'),
		('in_progress', 'In Progress'),
		('completed', 'Completed'),
		('failed', 'Failed'),
	], string="Status", default="pending")
	sync_date = fields.Datetime("Last Sync")

	def test_connection(self):
		# Ensure this method exists
		return True

	@api.model
	def _auto_sync_to_shopify(self):
		"""Auto-sync Odoo products to Shopify (for cron jobs)"""
		_logger.info("Running auto Odoo to Shopify sync...")
		return self.sync_products_to_shopify()

	@api.model
	def sync_products_to_shopify(self, template_ids=None):
		"""
		Sync Odoo product templates (and their variants) to Shopify.

		Args:
			template_ids (list, optional): Specific product.template IDs to sync.
				If None, syncs templates modified since the last sync. Defaults to None.
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
		
		# Record sync start time
		sync_start_time = fields.Datetime.now()

		TemplateModel = self.env['product.template']
		templates_to_sync = None

		if template_ids:
			templates_to_sync = TemplateModel.browse(template_ids)
			_logger.info(f"Syncing {len(templates_to_sync)} specific product templates to Shopify.")
		else:
			last_sync_to_shopify = self.env['ir.config_parameter'].sudo().get_param('shopify.last_export_time')
			domain = [('active', '=', True)] # Always sync active templates

			if last_sync_to_shopify:
				# Find templates modified OR whose variants were modified since last sync
				domain = ['|',
						  ('write_date', '>', last_sync_to_shopify),
						  ('product_variant_ids.write_date', '>', last_sync_to_shopify)
						 ] + domain
				_logger.info(f"Searching for templates/variants modified since {last_sync_to_shopify}")
				templates_to_sync = TemplateModel.search(domain, limit=1) # Limit to 1 per cron run
				_logger.info(f"Found {len(templates_to_sync)} templates potentially modified since last sync.")
			else:
				# First-time sync or no timestamp found
				templates_to_sync = TemplateModel.search(domain, limit=1) # Limit to 1 per cron run
				_logger.info(f"First-time sync or no timestamp, processing first {len(templates_to_sync)} active templates.")

		if not templates_to_sync:
			_logger.info("No product templates found to sync.")
			# Update timestamp even if nothing synced to avoid re-checking unchanged records
			self.env['ir.config_parameter'].sudo().set_param('shopify.last_export_time', sync_start_time)
			return {"status": "success", "message": "No products needed syncing.", "created": 0, "updated": 0, "failed": 0}
		created, updated, failed = 0, 0, 0
		
		sync_errors = [] # Collect errors for summary

		for template in templates_to_sync:
			try:
				# Prepare template data (including variants) for Shopify API
				product_data = self._prepare_shopify_product_data(template)
				if not product_data: # Skip if preparation failed (e.g., no active variants)
					_logger.warning(f"Skipping template {template.name} (ID: {template.id}) due to preparation issues.")
					continue

				shopify_product_id = template.shopify_product_id
				response = None

				if shopify_product_id and shopify_product_id.isdigit():
					# --- Update Existing Product ---
					_logger.info(f"Attempting to update template '{template.name}' (Odoo ID: {template.id}) in Shopify (Shopify ID: {shopify_product_id}).")
					product_url = f"https://{shopify_store_url}/admin/api/2023-01/products/{shopify_product_id}.json"
					# Ensure 'id' is included for updates if needed by API, but usually it's in the URL
					# product_data['id'] = int(shopify_product_id) # Usually not needed for PUT
					payload = json.dumps({"product": product_data})
					response = requests.put(product_url, headers=headers, data=payload)

					if response.status_code == 200:
						updated += 1
						_logger.info(f"Successfully updated template '{template.name}' in Shopify (Shopify ID: {shopify_product_id}).")
						# Update Odoo variant IDs from response
						self._update_odoo_variant_ids(template, response.json().get("product", {}))
					else:
						failed += 1
						error_msg = f"Failed to update template '{template.name}' (Odoo ID: {template.id}, Shopify ID: {shopify_product_id}) in Shopify: {response.status_code} - {response.text}"
						_logger.error(error_msg)
						sync_errors.append(error_msg)

				else:
					# --- Create New Product ---
					_logger.info(f"Attempting to create template '{template.name}' (Odoo ID: {template.id}) in Shopify.")
					product_url = f"https://{shopify_store_url}/admin/api/2023-01/products.json"
					payload = json.dumps({"product": product_data})
					response = requests.post(product_url, headers=headers, data=payload)

					if response.status_code == 201: # Status code for successful creation
						created += 1
						shopify_product_resp = response.json().get("product", {})
						new_shopify_product_id = str(shopify_product_resp.get("id"))
						_logger.info(f"Successfully created template '{template.name}' in Shopify (New Shopify ID: {new_shopify_product_id}).")

						# Store Shopify Product ID on template and Variant IDs on variants
						template.write({"shopify_product_id": new_shopify_product_id})
						self._update_odoo_variant_ids(template, shopify_product_resp) # Pass response to map variants

					else:
						failed += 1
						error_msg = f"Failed to create template '{template.name}' (Odoo ID: {template.id}) in Shopify: {response.status_code} - {response.text}"
						_logger.error(error_msg)
						sync_errors.append(error_msg)

			except requests.exceptions.RequestException as e:
				failed += 1
				error_msg = f"Network error syncing template '{template.name}' (Odoo ID: {template.id}): {e}"
				_logger.exception(error_msg)
				sync_errors.append(error_msg)
			except json.JSONDecodeError as e:
				failed += 1
				error_msg = f"JSON error processing response for template '{template.name}' (Odoo ID: {template.id}): {e}. Response: {response.text if response else 'No response'}"
				_logger.exception(error_msg)
				sync_errors.append(error_msg)
			except Exception as e:
				failed += 1
				error_msg = f"Unexpected error syncing template '{template.name}' (Odoo ID: {template.id}): {e}"
				_logger.exception(error_msg)
				sync_errors.append(error_msg)
				
		# Update last sync timestamp only if the batch had no failures (or decide based on policy)
		# For robustness, update timestamp even with failures to avoid retrying the same failed items indefinitely in simple cron setup.
		# If a more robust queue system is used, only update on full success.
		self.env['ir.config_parameter'].sudo().set_param('shopify.last_export_time', sync_start_time)
		_logger.info(f"Updated last export time to {sync_start_time}")
		
		summary_message = f"Sync to Shopify completed: {created} created, {updated} updated, {failed} failed."
		_logger.info(summary_message)
		if sync_errors:
			_logger.error("Errors encountered during sync:\n" + "\n".join(sync_errors))
			return {
				"status": "error" if failed > 0 else "success", # Consider partial success?
				"message": summary_message + "\nErrors:\n" + "\n".join(sync_errors),
				"created": created,
				"updated": updated,
				"failed": failed
			}
		else:
			return {
				"status": "success",
				"message": summary_message,
				"created": created,
				"updated": updated,
				"failed": failed
			}
	
	def _prepare_shopify_product_data(self, template):
		"""
		Prepare Odoo product template data (including variants) for the Shopify API.

		Args:
			template (product.template): The Odoo product template record.

		Returns:
			dict or None: Formatted product data for Shopify, or None if no active variants.
		"""
		shopify_product_status = self.env['ir.config_parameter'].sudo().get_param('shopify.product_status', 'draft')
		# is_published = shopify_product_status == 'active' # 'status' field handles this now

		# --- Prepare Variants ---
		shopify_variants_data = []
		active_odoo_variants = template.product_variant_ids.filtered(lambda v: v.active)

		if not active_odoo_variants:
			_logger.warning(f"Template '{template.name}' (ID: {template.id}) has no active variants. Skipping.")
			return None # Cannot sync a product with no variants

		for variant in active_odoo_variants:
			# Use a fallback SKU if default_code is empty, essential for matching later
			variant_sku = variant.default_code or f"odoo-{template.id}-{variant.id}"
			# --- Minimal Variant Data for Debugging ---
			variant_data = {
				"price": str(variant.list_price),
				"sku": variant_sku, # Keep SKU for matching
				"inventory_management": "shopify", # Assuming Shopify manages inventory
				"inventory_quantity": self._get_product_stock_quantity(variant),
				"requires_shipping": True, # Assuming physical products
				"barcode": variant.barcode or "",
				"weight": variant.weight,
				"weight_unit": "kg", # Assuming kg, adjust if needed
			}
			# If updating, include the Shopify variant ID for matching
			if variant.shopify_variant_id and variant.shopify_variant_id.isdigit():
				variant_data["id"] = int(variant.shopify_variant_id)

			# --- Add Option Values ---
			option_values = variant.product_template_attribute_value_ids.mapped('name')
			if len(option_values) > 0: variant_data['option1'] = option_values[0]
			if len(option_values) > 1: variant_data['option2'] = option_values[1]
			if len(option_values) > 2: variant_data['option3'] = option_values[2]
			# Note: Shopify only supports up to 3 options.

			shopify_variants_data.append(variant_data)

		# --- Prepare Main Product Data ---
		# --- Minimal Product Data for Debugging ---
		product_data = {
			"title": template.name,
			"body_html": template.description_sale or template.name,
			"vendor": template.company_id.name if template.company_id else "Odoo",
			"product_type": template.categ_id.name if template.categ_id else "Uncategorized",
			"status": shopify_product_status, # Use configured status (draft/active)
			"tags": ", ".join(template.tag_ids.mapped('name')),
			"variants": shopify_variants_data,
		}

		# --- Add Options ---
		options = []
		# Limit to first 3 attribute lines as Shopify supports max 3 options
		for attr_line in template.attribute_line_ids[:3]:
			options.append({
				"name": attr_line.attribute_id.name,
				"values": list(attr_line.value_ids.mapped('name'))
			})
		if options:
			product_data["options"] = options

		# --- Add Images (Optional, uncomment if needed) ---
		# images_data = []
		# if template.image_1920:
		# 	try:
		# 		# Ensure image data is bytes before encoding
		# 		img_bytes = template.image_1920 if isinstance(template.image_1920, bytes) else base64.b64decode(template.image_1920)
		# 		img_b64 = base64.b64encode(img_bytes).decode('utf-8')
		# 		images_data.append({"attachment": img_b64})
		# 	except Exception as e:
		# 		_logger.error(f"Could not encode image for template {template.name}: {e}")
		#
		# if images_data:
		# 	product_data["images"] = images_data

		# If updating, include the Shopify product ID in the main dict (required by Shopify PUT)
		if template.shopify_product_id and template.shopify_product_id.isdigit():
			product_data['id'] = int(template.shopify_product_id)

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
		
		# Ensure quantity is non-negative
		return max(0, int(sum(quants.mapped('quantity'))))

	def _update_odoo_variant_ids(self, odoo_template, shopify_product_resp):
		"""
		Update Odoo product.product records with Shopify variant IDs based on SKU matching.

		Args:
			odoo_template (product.template): The Odoo template record.
			shopify_product_resp (dict): The product data dictionary from Shopify API response.
		"""
		shopify_variants = shopify_product_resp.get('variants', [])
		if not shopify_variants:
			_logger.warning(f"No variants found in Shopify response for template '{odoo_template.name}' (Odoo ID: {odoo_template.id})")
			return

		_logger.info(f"Mapping {len(shopify_variants)} Shopify variants to Odoo variants for template '{odoo_template.name}'.")

		# Create a map of Odoo variants by SKU (using the same fallback logic as _prepare_shopify_product_data)
		odoo_variants_map = {}
		for v in odoo_template.product_variant_ids:
			sku = v.default_code or f"odoo-{odoo_template.id}-{v.id}"
			odoo_variants_map[sku] = v

		updated_count = 0
		missing_sku_count = 0
		for shopify_variant in shopify_variants:
			shopify_variant_id = str(shopify_variant.get("id"))
			sku = shopify_variant.get("sku")

			if not sku:
				_logger.warning(f"Shopify variant ID {shopify_variant_id} for template '{odoo_template.name}' is missing SKU in response. Cannot map.")
				missing_sku_count += 1
				continue

			odoo_variant = odoo_variants_map.get(sku)

			if odoo_variant:
				# Check if ID needs updating or is missing
				if odoo_variant.shopify_variant_id != shopify_variant_id:
					try:
						# Use sudo() if cron job runs as a restricted user
						odoo_variant.sudo().write({"shopify_variant_id": shopify_variant_id})
						updated_count += 1
						_logger.debug(f"Mapped Shopify variant ID {shopify_variant_id} (SKU: {sku}) to Odoo variant ID {odoo_variant.id}.")
					except Exception as e:
						_logger.error(f"Failed to write Shopify Variant ID {shopify_variant_id} to Odoo variant {odoo_variant.id} (SKU: {sku}): {e}")
						# Consider adding to sync_errors list?
				# else: # No update needed
				#     _logger.debug(f"Shopify variant ID {shopify_variant_id} (SKU: {sku}) already matches Odoo variant ID {odoo_variant.id}.")
			else:
				_logger.warning(f"Could not find matching Odoo variant for Shopify variant ID {shopify_variant_id} with SKU '{sku}' in template '{odoo_template.name}'. Was the variant deleted or SKU changed in Odoo without syncing?")

		log_message = f"Finished mapping variants for template '{odoo_template.name}'. Updated: {updated_count}."
		if missing_sku_count > 0:
			log_message += f" Skipped {missing_sku_count} Shopify variants due to missing SKU in response."
		_logger.info(log_message)

class ProductProductShopify(models.Model):
	_inherit = "product.product"

	shopify_product_id = fields.Char("Shopify Product ID", related='product_tmpl_id.shopify_product_id', store=False, readonly=True, help="Technical field: Shopify ID of the parent template.")
	shopify_variant_id = fields.Char("Shopify Variant ID", index=True, copy=False, help="ID of this specific variant in Shopify")
	
	def sync_template_to_shopify(self):
		"""Sync the parent template of this variant to Shopify"""
		templates = self.mapped('product_tmpl_id')
		if templates:
			return self.env['odoo.sync'].sudo().sync_products_to_shopify(template_ids=templates.ids)
		return False
	
	# Note: Automatic sync on create/write is disabled. Use the cron job or manual sync.
	# If automatic sync is desired, uncomment and adapt the following methods
	# to call sync_template_to_shopify() for the affected templates.

# Add shopify_product_id to product.template
class ProductTemplateShopify(models.Model):
	_inherit = "product.template"

	shopify_product_id = fields.Char("Shopify Product ID", index=True, copy=False, help="ID of the product in Shopify")

	def sync_to_shopify(self):
		"""Sync this product template to Shopify"""
		return self.env['odoo.sync'].sudo().sync_products_to_shopify(template_ids=self.ids)
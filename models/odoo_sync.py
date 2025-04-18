# File: odoo_sync.py
import logging
import requests
import json
import base64
import time # <-- Add import
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
        sync_errors = []

        # --- Get Location Mapping (Attempt to reuse from shopify.sync) ---
        # This mapping should be Shopify Location ID -> Odoo *Stock* Location ID
        # The function in shopify_sync maps to Warehouse Stock Loc ID, which is correct.
        location_mapping = {}
        try:
            # Use sudo() if the cron user might not have direct access to shopify.sync model/methods
            location_mapping = self.env['shopify.sync'].sudo()._get_odoo_shopify_location_mapping()
            if not location_mapping:
                 _logger.warning("Odoo->Shopify Sync: Could not retrieve Shopify Location ID -> Odoo Stock Location ID mapping. Inventory sync will be skipped.")
            else:
                 _logger.info(f"Odoo->Shopify Sync: Using location mapping: {location_mapping}")
        except Exception as map_e:
            _logger.error(f"Odoo->Shopify Sync: Failed to get location mapping: {map_e}. Inventory sync will be skipped.")
            sync_errors.append(f"Failed to get location mapping: {map_e}")
        # --- End Location Mapping ---


        for template in templates_to_sync:
            try:
                # Prepare template data (excluding inventory quantity)
                product_data = self._prepare_shopify_product_data(template)
                if not product_data: # Skip if preparation failed (e.g., no active variants)
                    _logger.warning(f"Skipping template {template.name} (ID: {template.id}) due to preparation issues.")
                    continue

                shopify_product_id = template.shopify_product_id
                response = None
                sync_successful = False # Flag to track if product sync worked

                if shopify_product_id and shopify_product_id.isdigit():
                    # --- Update Existing Product ---
                    _logger.info(f"Attempting to update template '{template.name}' (Odoo ID: {template.id}) in Shopify (Shopify ID: {shopify_product_id}).")
                    product_url = f"https://{shopify_store_url}/admin/api/2023-01/products/{shopify_product_id}.json"
                    payload = json.dumps({"product": product_data})
                    response = requests.put(product_url, headers=headers, data=payload, timeout=30) # Added timeout

                    if response.status_code == 200:
                        updated += 1
                        _logger.info(f"Successfully updated template '{template.name}' in Shopify (Shopify ID: {shopify_product_id}).")
                        # Update Odoo variant IDs (including inventory_item_id) from response
                        self._update_odoo_variant_ids(template, response.json().get("product", {}))
                        sync_successful = True # Mark as successful for inventory sync
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
                    response = requests.post(product_url, headers=headers, data=payload, timeout=30) # Added timeout

                    if response.status_code == 201: # Status code for successful creation
                        created += 1
                        shopify_product_resp = response.json().get("product", {})
                        new_shopify_product_id = str(shopify_product_resp.get("id"))
                        _logger.info(f"Successfully created template '{template.name}' in Shopify (New Shopify ID: {new_shopify_product_id}).")

                        # Store Shopify Product ID on template
                        template.write({"shopify_product_id": new_shopify_product_id})
                        # Update Odoo variant IDs (including inventory_item_id) from response
                        self._update_odoo_variant_ids(template, shopify_product_resp)
                        sync_successful = True # Mark as successful for inventory sync
                    else:
                        failed += 1
                        error_msg = f"Failed to create template '{template.name}' (Odoo ID: {template.id}) in Shopify: {response.status_code} - {response.text}"
                        _logger.error(error_msg)
                        sync_errors.append(error_msg)

                # --- Sync Inventory Levels (if product sync was successful and mapping exists) ---
                if sync_successful and location_mapping:
                    inventory_sync_result = self._sync_inventory_levels_to_shopify(template, location_mapping, headers, shopify_store_url)
                    if not inventory_sync_result["status"]:
                        # Decide if inventory failure should mark the whole product sync as failed
                        # For now, log errors and add to sync_errors, but don't increment 'failed' count for the product itself
                        sync_errors.extend(inventory_sync_result["errors"])
                        _logger.error(f"Inventory sync failed for template '{template.name}' (Odoo ID: {template.id}). Errors: {inventory_sync_result['errors']}")
                elif sync_successful and not location_mapping:
                     _logger.warning(f"Product sync successful for '{template.name}', but skipping inventory sync due to missing location mapping.")
                # --- End Inventory Sync ---

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
        Prepare Odoo product template data (excluding inventory quantity) for the Shopify API.

        Args:
            template (product.template): The Odoo product template record.

        Returns:
            dict or None: Formatted product data for Shopify, or None if no active variants.
        """
        shopify_product_status = self.env['ir.config_parameter'].sudo().get_param('shopify.product_status', 'draft')
        # --- Prepare Variants ---
        shopify_variants_data = []
        active_odoo_variants = template.product_variant_ids.filtered(lambda v: v.active)

        if not active_odoo_variants:
            _logger.warning(f"Template '{template.name}' (ID: {template.id}) has no active variants. Skipping.")
            return None # Cannot sync a product with no variants

        for variant in active_odoo_variants:
            # Use a fallback SKU if default_code is empty, essential for matching later
            variant_sku = variant.default_code or f"odoo-{template.id}-{variant.id}"
            # --- Minimal Variant Data (NO INVENTORY QTY) ---
            variant_data = {
                "price": str(variant.list_price),
                "sku": variant_sku, # Keep SKU for matching
                "inventory_management": "shopify", # Let Shopify manage inventory levels via API
                # "inventory_quantity": 0, # DO NOT SEND - Use inventory_levels/set API instead
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
        product_data = {
            "title": template.name,
            "body_html": template.description_sale or template.name,
            "vendor": template.company_id.name if template.company_id else "Odoo",
            "product_type": template.categ_id.name if template.categ_id else "Uncategorized",
            "status": shopify_product_status, # Use configured status (draft/active)
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

        # --- Add Images (Optional, handle if needed) ---
        # ... image handling logic would go here ...

        # If updating, include the Shopify product ID in the main dict
        if template.shopify_product_id and template.shopify_product_id.isdigit():
            product_data['id'] = int(template.shopify_product_id)

        return product_data

    def _get_product_stock_quantity(self, product):
        """
        Get TOTAL available stock quantity for a product across all internal locations.
        NOTE: This is NOT used for syncing inventory per location to Shopify anymore.
        """
        quants = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id.usage', '=', 'internal')
        ])
        return max(0, int(sum(quants.mapped('quantity'))))

    # --- NEW: Helper to get stock for a specific location ---
    def _get_product_stock_for_location(self, product, location_id):
        """
        Get available 'quantity' (on hand) for a product in a specific Odoo stock.location.

        Args:
            product (product.product): Odoo product.product record.
            location_id (int): ID of the specific Odoo stock.location.

        Returns:
            int: Available quantity in that location.
        """
        # We need the on-hand quantity in this specific location.
        # Using read_group is efficient for summing quantities.
        stock_data = self.env['stock.quant'].read_group(
            [('product_id', '=', product.id), ('location_id', '=', location_id)],
            ['quantity:sum'], # Field to sum
            [] # No grouping needed, just sum for the filtered records
        )
        # read_group returns a list of dicts, e.g., [{'quantity': 10.0, '__count': 1}]
        # or an empty list if no quants found for that product/location.
        quantity = 0
        if stock_data and stock_data[0].get('quantity') is not None: # Check if quantity key exists and is not None
             quantity = stock_data[0]['quantity']

        # Ensure non-negative integer quantity
        return max(0, int(quantity))


    def _update_odoo_variant_ids(self, odoo_template, shopify_product_resp):
        """
        Update Odoo product.product records with Shopify variant IDs and inventory item IDs
        based on SKU matching from the Shopify API response.

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
            shopify_inventory_item_id = str(shopify_variant.get("inventory_item_id")) # Get inventory item ID
            sku = shopify_variant.get("sku")

            if not sku:
                _logger.warning(f"Shopify variant ID {shopify_variant_id} for template '{odoo_template.name}' is missing SKU in response. Cannot map.")
                missing_sku_count += 1
                continue

            odoo_variant = odoo_variants_map.get(sku)

            if odoo_variant:
                # Check if IDs need updating or are missing
                update_vals = {}
                if odoo_variant.shopify_variant_id != shopify_variant_id:
                    update_vals["shopify_variant_id"] = shopify_variant_id
                # Also update inventory_item_id if missing or changed
                if odoo_variant.shopify_inventory_item_id != shopify_inventory_item_id:
                    update_vals["shopify_inventory_item_id"] = shopify_inventory_item_id

                if update_vals:
                    try:
                        # Use sudo() if cron job runs as a restricted user
                        odoo_variant.sudo().write(update_vals)
                        updated_count += 1
                        _logger.debug(f"Mapped Shopify IDs for SKU {sku} to Odoo variant ID {odoo_variant.id}: {update_vals}")
                    except Exception as e:
                        _logger.error(f"Failed to write Shopify IDs {update_vals} to Odoo variant {odoo_variant.id} (SKU: {sku}): {e}")
                        # Consider adding to sync_errors list?
            else:
                _logger.warning(f"Could not find matching Odoo variant for Shopify variant ID {shopify_variant_id} with SKU '{sku}' in template '{odoo_template.name}'. Was the variant deleted or SKU changed in Odoo without syncing?")

        log_message = f"Finished mapping variants for template '{odoo_template.name}'. Updated: {updated_count}."
        if missing_sku_count > 0:
            log_message += f" Skipped {missing_sku_count} Shopify variants due to missing SKU in response."
        _logger.info(log_message)


    # --- NEW: Function to sync inventory levels using inventory_levels/set ---
    def _sync_inventory_levels_to_shopify(self, template, location_mapping, headers, shopify_store_url):
        """
        Syncs inventory levels for all variants of a template to Shopify using the
        inventory_levels/set endpoint for mapped locations.

        Args:
            template (product.template): The Odoo product template record.
            location_mapping (dict): Mapping {shopify_location_id (str): odoo_stock_location_id (int)}.
            headers (dict): HTTP headers for Shopify API calls.
            shopify_store_url (str): The Shopify store URL.

        Returns:
            dict: {"status": bool, "errors": list}
        """
        _logger.info(f"Starting inventory level sync for template '{template.name}' (ID: {template.id})")
        errors = []
        overall_success = True

        # Invert mapping for easier lookup: Odoo Stock Loc ID (int) -> Shopify Loc ID (str)
        odoo_to_shopify_loc_map = {v: k for k, v in location_mapping.items()}

        for variant in template.product_variant_ids.filtered(lambda v: v.active):
            inventory_item_id = variant.shopify_inventory_item_id
            sku = variant.default_code or f"odoo-{template.id}-{variant.id}"

            if not inventory_item_id or not inventory_item_id.isdigit():
                _logger.warning(f"  Skipping inventory sync for variant SKU '{sku}' (Odoo ID: {variant.id}) - Missing or invalid Shopify Inventory Item ID ('{inventory_item_id}').")
                continue # Skip this variant if we don't have the ID needed for the API call

            _logger.debug(f"  Processing inventory for variant SKU '{sku}' (Inv Item ID: {inventory_item_id})")

            # Iterate through Odoo locations that are mapped to Shopify locations
            for odoo_loc_id, shopify_loc_id in odoo_to_shopify_loc_map.items():
                try:
                    # Get stock specifically for this Odoo location
                    available_qty = self._get_product_stock_for_location(variant, odoo_loc_id)
                    _logger.info(f"    Syncing SKU '{sku}' to Shopify Loc ID {shopify_loc_id} (Odoo Loc ID: {odoo_loc_id}) with Qty: {available_qty}")

                    inventory_url = f"https://{shopify_store_url}/admin/api/2023-01/inventory_levels/set.json"
                    payload = json.dumps({
                        "location_id": int(shopify_loc_id), # Ensure integer
                        "inventory_item_id": int(inventory_item_id), # Ensure integer
                        "available": available_qty,
                        # "disconnect_if_necessary": False # Optional: Set to true to disconnect from other fulfillment services if needed
                    })

                    # Use a slightly shorter timeout for potentially faster inventory calls
                    response = requests.post(inventory_url, headers=headers, data=payload, timeout=20)

                    # Handle Rate Limiting (429)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get('Retry-After', 5)) # Default to 5 seconds
                        _logger.warning(f"    Rate limit hit syncing inventory for SKU '{sku}' at Loc {shopify_loc_id}. Waiting {retry_after} seconds.")
                        time.sleep(retry_after)
                        # Retry the same request
                        response = requests.post(inventory_url, headers=headers, data=payload, timeout=20)

                    # Check for success (200 OK for inventory level set)
                    if response.status_code != 200:
                        error_msg = f"    Failed to set inventory for SKU '{sku}' (Inv Item ID: {inventory_item_id}) at Shopify Loc {shopify_loc_id}: {response.status_code} - {response.text}"
                        _logger.error(error_msg)
                        errors.append(error_msg)
                        overall_success = False
                    # else: # Log success if needed
                    #    _logger.debug(f"    Successfully set inventory for SKU '{sku}' at Shopify Loc {shopify_loc_id} to {available_qty}.")

                except requests.exceptions.RequestException as e:
                    error_msg = f"    Network error syncing inventory for SKU '{sku}' at Shopify Loc {shopify_loc_id}: {e}"
                    _logger.exception(error_msg) # Log stack trace for network errors
                    errors.append(error_msg)
                    overall_success = False
                except Exception as e:
                    error_msg = f"    Unexpected error syncing inventory for SKU '{sku}' at Shopify Loc {shopify_loc_id}: {e}"
                    _logger.exception(error_msg) # Log stack trace for unexpected errors
                    errors.append(error_msg)
                    overall_success = False

        _logger.info(f"Finished inventory level sync for template '{template.name}'. Success: {overall_success}")
        return {"status": overall_success, "errors": errors}


# --- Model Extensions ---
class ProductProductShopify(models.Model):
    _inherit = "product.product"

    shopify_product_id = fields.Char("Shopify Product ID", related='product_tmpl_id.shopify_product_id', store=False, readonly=True, help="Technical field: Shopify ID of the parent template.")
    shopify_variant_id = fields.Char("Shopify Variant ID", index=True, copy=False, help="ID of this specific variant in Shopify")
    shopify_inventory_item_id = fields.Char("Shopify Inventory Item ID", index=True, copy=False, help="Inventory Item ID used for stock level updates in Shopify")

    def sync_template_to_shopify(self):
        """Sync the parent template of this variant to Shopify"""
        templates = self.mapped('product_tmpl_id')
        if templates:
            # Use sudo() for potential permission issues if called by different users/cron
            return self.env['odoo.sync'].sudo().sync_products_to_shopify(template_ids=templates.ids)
        return False

    # Note: Automatic sync on create/write is disabled. Use the cron job or manual sync.

class ProductTemplateShopify(models.Model):
    _inherit = "product.template"

    shopify_product_id = fields.Char("Shopify Product ID", index=True, copy=False, help="ID of the product in Shopify")

    def sync_to_shopify(self):
        """Sync this product template to Shopify"""
        # Use sudo() for potential permission issues
        return self.env['odoo.sync'].sudo().sync_products_to_shopify(template_ids=self.ids)
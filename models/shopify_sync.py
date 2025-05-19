import logging
import requests
import base64
import time
from datetime import datetime
from dateutil import parser
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# --- Weight Conversion Helper ---
def _convert_weight_to_kg(value, unit):
    """Converts weight value from Shopify unit to KG."""
    unit = unit.lower()
    if unit == 'g':
        return value / 1000.0
    elif unit == 'oz':
        return value * 0.0283495
    elif unit == 'lb':
        return value * 0.453592
    elif unit == 'kg':
        return value
    else:
        _logger.warning(f"Unsupported weight unit '{unit}'. Assuming KG.")
        return value
# --- End Helper ---

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
    def _get_odoo_shopify_location_mapping(self):
        """
        Fetches Shopify locations and maps them to Odoo stock.location records.
        Creates Odoo locations if they don't exist based on shopify_location_id.
        Creates Odoo Warehouses and their locations if they don't exist based on shopify_location_id.
        Maps Shopify Location ID -> Odoo Warehouse's primary Stock Location ID.
        Caches the mapping in the context for the duration of the sync.

        Returns:
            dict: Mapping {shopify_location_id (str): odoo_stock_location_id (int)}
        """
        cache_key = '_shopify_warehouse_stock_loc_mapping_cache' # Renamed cache key
        if cache_key in self.env.context:
            _logger.info("Using cached Shopify location -> Odoo WH Stock Loc mapping.")
            return self.env.context[cache_key]

        _logger.info("Fetching Shopify locations and building Warehouse/Stock Loc mapping...")
        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')
        WarehouseModel = self.env['stock.warehouse']
        location_mapping = {} # Maps Shopify Loc ID -> Odoo WH Stock Loc ID

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials for location fetching.")
            return {}

        location_url = f"https://{shopify_store_url}/admin/api/2023-01/locations.json"
        headers = {"X-Shopify-Access-Token": shopify_access_token}

        try:
            response = requests.get(location_url, headers=headers, timeout=15)
            response.raise_for_status()

            shopify_locations = response.json().get("locations", [])
            _logger.info(f"Fetched {len(shopify_locations)} locations from Shopify.")

            for shopify_loc in shopify_locations:
                shopify_loc_id_str = str(shopify_loc['id'])
                shopify_loc_name = shopify_loc['name']

                # Search for existing Odoo Warehouse by Shopify ID
                odoo_warehouse = WarehouseModel.search([('shopify_location_id', '=', shopify_loc_id_str)], limit=1)

                if not odoo_warehouse:
                    # If not found by ID, try searching by name (less reliable)
                    odoo_warehouse = WarehouseModel.search([
                        ('name', '=', shopify_loc_name),
                        ('shopify_location_id', '=', False) # Only match if not already linked
                    ], limit=1)

                    if odoo_warehouse:
                         # Found by name, link it by setting the Shopify ID
                         try:
                             odoo_warehouse.sudo().write({'shopify_location_id': shopify_loc_id_str})
                             _logger.info(f"Linked existing Odoo Warehouse '{shopify_loc_name}' (ID: {odoo_warehouse.id}) to Shopify Location ID {shopify_loc_id_str}.")
                         except Exception as link_e:
                             _logger.error(f"Failed to link Odoo Warehouse ID {odoo_warehouse.id} to Shopify ID {shopify_loc_id_str}: {link_e}")
                             odoo_warehouse = False # Ensure we don't use this if linking failed
                    else:
                         # Create a new Odoo Warehouse
                         # Generate a short code (e.g., first 5 chars of name, uppercase)
                         short_code = ''.join(filter(str.isalnum, shopify_loc_name))[:5].upper()
                         if not short_code: # Handle empty or non-alphanumeric names
                             short_code = f"SH{shopify_loc_id_str[-3:]}" # Fallback code

                         # Check if code already exists
                         code_count = WarehouseModel.search_count([('code', '=', short_code)])
                         if code_count > 0:
                             short_code = f"{short_code[:3]}{shopify_loc_id_str[-2:]}" # Make it more unique
                             _logger.warning(f"Generated warehouse code '{short_code}' already exists. Using fallback: {short_code}")

                         try:
                             create_vals = {
                                 'name': shopify_loc_name,
                                 'code': short_code,
                                 'shopify_location_id': shopify_loc_id_str,
                                 'company_id': self.env.company.id,
                             }
                             odoo_warehouse = WarehouseModel.sudo().create(create_vals)
                             _logger.info(f"Created new Odoo Warehouse '{odoo_warehouse.name}' (ID: {odoo_warehouse.id}, Code: {odoo_warehouse.code}) for Shopify Location ID {shopify_loc_id_str}.")
                         except Exception as create_e:
                             _logger.error(f"Failed to create Odoo Warehouse for Shopify Location ID {shopify_loc_id_str} (Name: {shopify_loc_name}): {create_e}")
                             continue # Skip this location if creation fails

                # Map Shopify Loc ID to the Odoo Warehouse's main Stock Location ID
                if odoo_warehouse and odoo_warehouse.lot_stock_id:
                    location_mapping[shopify_loc_id_str] = odoo_warehouse.lot_stock_id.id
                elif odoo_warehouse:
                     _logger.warning(f"Warehouse {odoo_warehouse.name} (ID: {odoo_warehouse.id}) found or created, but it has no main stock location (lot_stock_id). Cannot map.")
                else:
                     _logger.warning(f"Could not find or create a usable Odoo Warehouse for Shopify Loc ID {shopify_loc_id_str}. Cannot map.")


        except requests.exceptions.RequestException as e:
            _logger.error(f"Error fetching Shopify locations: {e}")
        except Exception as e:
             _logger.exception(f"Unexpected error processing Shopify locations: {e}")


        # Cache the result in the context
        self.env.context = dict(self.env.context, **{cache_key: location_mapping})
        _logger.info(f"Built and cached Shopify Loc ID -> Odoo WH Stock Loc ID mapping: {location_mapping}") # Keep this final summary log
        return location_mapping


    @api.model
    def _fetch_shopify_inventory_levels(self, inventory_item_ids):
        """
        Fetches inventory levels for a list of inventory_item_ids from Shopify in a single call.

        Args:
            inventory_item_ids (list[str | int]): A list of Shopify inventory item IDs.

        Returns:
            dict: A dictionary mapping inventory_item_id (str) to its list of inventory levels
                  (e.g., [{'location_id': ..., 'available': ...}, ...]).
                  Returns an empty dict if no IDs are provided or on error.
        """
        if not inventory_item_ids:
            _logger.warning("Cannot fetch inventory levels without inventory_item_ids.")
            return {}

        # Ensure all IDs are strings for joining and dictionary keys
        item_ids_str_list = [str(item_id) for item_id in inventory_item_ids if item_id]
        if not item_ids_str_list:
             _logger.warning("No valid inventory_item_ids provided after filtering.")
             return {}

        item_ids_param = ",".join(item_ids_str_list)

        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials for inventory level fetching.")
            return {}

        # Consider pagination if fetching for a very large number of items might exceed URL length limits or response size limits.
        # For now, assume a reasonable number of variants per product fits in one call. Max limit is 250 for inventory_levels.json.
        # If item_ids_str_list is > 250, we'd need to batch this call too.
        if len(item_ids_str_list) > 250:
             _logger.warning(f"Attempting to fetch inventory levels for {len(item_ids_str_list)} items, which exceeds the Shopify limit of 250 per call. Fetching only the first 250.")
             item_ids_param = ",".join(item_ids_str_list[:250])
             # TODO: Implement proper batching here if needed for > 250 variants per product

        inventory_url = f"https://{shopify_store_url}/admin/api/2023-01/inventory_levels.json"
        headers = {"X-Shopify-Access-Token": shopify_access_token}
        # Limit can be up to 250 for inventory_levels
        params = {"inventory_item_ids": item_ids_param, "limit": 250}

        all_inventory_levels = []
        try:
            _logger.info(f"Fetching inventory levels for {len(item_ids_str_list)} item IDs: {item_ids_param[:200]}...") # Log truncated list
            # Increase timeout slightly for potentially larger response
            response = requests.get(inventory_url, headers=headers, params=params, timeout=30)

            if response.status_code == 429:
                 retry_after = int(response.headers.get('Retry-After', 10))
                 _logger.warning(f"Rate limit hit fetching inventory for items {item_ids_param[:100]}..., waiting {retry_after} seconds")
                 time.sleep(retry_after)
                 # Retry the same batch
                 return self._fetch_shopify_inventory_levels(inventory_item_ids)

            response.raise_for_status() # Raise HTTPError for other bad responses

            data = response.json()
            all_inventory_levels = data.get("inventory_levels", [])
            _logger.info(f"Received {len(all_inventory_levels)} total inventory level records for the requested item IDs.")

        except requests.exceptions.RequestException as e:
            _logger.error(f"Error fetching Shopify inventory levels for item IDs {item_ids_param[:100]}...: {e}")
            return {} # Return empty dict on error
        except Exception as e:
             _logger.exception(f"Unexpected error processing Shopify inventory levels for item IDs {item_ids_param[:100]}...: {e}")
             return {} # Return empty dict on error

        # Group levels by inventory_item_id
        levels_by_item_id = {}
        for level in all_inventory_levels:
            item_id_str = str(level.get('inventory_item_id'))
            if item_id_str not in levels_by_item_id:
                levels_by_item_id[item_id_str] = []
            levels_by_item_id[item_id_str].append(level)

        # Ensure all requested IDs have an entry (even if empty)
        for item_id in item_ids_str_list:
            if item_id not in levels_by_item_id:
                levels_by_item_id[item_id] = []

        return levels_by_item_id


    @api.model
    def _fetch_shopify_inventory_item_costs_batch(self, inventory_item_ids):
        """
        Fetches costs for a list of inventory_item_ids from Shopify using a batch request.

        Args:
            inventory_item_ids (list[str | int]): A list of Shopify inventory item IDs.

        Returns:
            dict: A dictionary mapping inventory_item_id (str) to its cost (float).
                  Returns an empty dict if no IDs are provided or on error.
        """
        if not inventory_item_ids:
            _logger.warning("Cannot fetch inventory item costs without inventory_item_ids.")
            return {}

        # Ensure all IDs are strings and filter out any None/empty values
        item_ids_str_list = [str(item_id) for item_id in inventory_item_ids if item_id]
        if not item_ids_str_list:
             _logger.warning("No valid inventory_item_ids provided after filtering for cost fetching.")
             return {}

        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials for inventory item cost fetching.")
            return {}

        costs_by_item_id = {}
        # Shopify typically limits 'ids' parameter to 100 or 250. Let's use 100 to be safe.
        batch_size = 100
        for i in range(0, len(item_ids_str_list), batch_size):
            batch_ids = item_ids_str_list[i:i + batch_size]
            item_ids_param = ",".join(batch_ids)
            item_url = f"https://{shopify_store_url}/admin/api/2023-01/inventory_items.json"
            headers = {"X-Shopify-Access-Token": shopify_access_token}
            params = {"ids": item_ids_param, "limit": batch_size} # Use limit matching batch size

            try:
                _logger.info(f"Fetching batch of inventory item costs ({len(batch_ids)} IDs): {item_ids_param[:200]}...")
                response = requests.get(item_url, headers=headers, params=params, timeout=30) # Increased timeout

                if response.status_code == 429:
                     retry_after = int(response.headers.get('Retry-After', 10))
                     _logger.warning(f"Rate limit hit fetching inventory item costs batch, waiting {retry_after} seconds")
                     time.sleep(retry_after)
                     # Retry the same batch by recursively calling (or could implement iterative retry)
                     # Note: This recursive call needs careful handling in case of persistent errors
                     batch_costs = self._fetch_shopify_inventory_item_costs_batch(batch_ids)
                     costs_by_item_id.update(batch_costs)
                     continue # Move to the next batch after successful retry

                response.raise_for_status() # Raise HTTPError for other bad responses

                data = response.json()
                inventory_items = data.get("inventory_items", [])
                _logger.info(f"  Received {len(inventory_items)} inventory items in cost batch response.")

                for item in inventory_items:
                    item_id_str = str(item.get("id"))
                    cost_str = item.get("cost")
                    cost = 0.0
                    if cost_str is not None:
                        try:
                            cost = float(cost_str)
                        except (ValueError, TypeError):
                             _logger.warning(f"  Could not convert cost '{cost_str}' to float for item ID {item_id_str} in batch.")
                    else:
                        _logger.debug(f"  'cost' field not found for item ID {item_id_str} in batch.") # Debug level might be better
                    costs_by_item_id[item_id_str] = cost

            except requests.exceptions.Timeout:
                _logger.warning(f"Timeout fetching Shopify inventory item costs batch starting with ID {batch_ids[0]}. Skipping batch.")
            except requests.exceptions.RequestException as e:
                _logger.error(f"Error fetching Shopify inventory item costs batch starting with ID {batch_ids[0]}: {e}. Skipping batch.")
            except Exception as e:
                 _logger.exception(f"Unexpected error processing Shopify inventory item costs batch starting with ID {batch_ids[0]}: {e}")

        _logger.info(f"Finished fetching costs for {len(costs_by_item_id)} / {len(item_ids_str_list)} requested item IDs.")
        return costs_by_item_id

    @api.model
    def _auto_sync_products(self):
        """Auto-sync Shopify products (for cron jobs)"""
        _logger.info("Running auto Shopify sync...")
        return self.sync_products_to_odoo()

    @api.model
    def fetch_shopify_products(self, last_sync_time=None, batch_size=10):
        """
        Fetch Shopify products with pagination, sorted by `updated_at` ascending

        Args:
            last_sync_time: Only fetch products updated since this time
            batch_size: Number of products to fetch per batch (default: 50)
        """
        shopify_access_token = self.env['ir.config_parameter'].sudo().get_param('shopify.access_token')
        shopify_store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials. Ensure they are set in system parameters.")
            return []

        base_url = f"https://{shopify_store_url}/admin/api/2023-01/products.json"
        headers = {"X-Shopify-Access-Token": shopify_access_token}

        params = {
            "limit": batch_size,
            "order": "updated_at asc",
            # "status": "active" # Fetch only active products
        }

        if last_sync_time:
            _logger.info(f"Filtering products updated since: {last_sync_time}")
            params["updated_at_min"] = last_sync_time

        all_products = []

        try:
            _logger.info(f"Fetching Shopify products batch (max {batch_size})")
            if base_url.startswith('<') and base_url.endswith('>'):
                base_url = base_url[1:-1]

            response = requests.get(base_url, headers=headers, params=params)

            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 10))
                _logger.warning(f"Rate limit hit, waiting {retry_after} seconds")
                time.sleep(retry_after)
                return self.fetch_shopify_products(last_sync_time, batch_size)

            elif response.status_code != 200:
                _logger.error(f"Shopify API Error: {response.status_code} - {response.text}")
                return []

            data = response.json()
            all_products = data.get("products", [])
            _logger.info(f"Retrieved {len(all_products)} products in this batch")

            link_header = response.headers.get("Link", "")
            if link_header and 'rel="next"' in link_header:
                for link in link_header.split(','):
                    if 'rel="next"' in link:
                        url_part = link.split(';')[0].strip()
                        if url_part.startswith('<') and url_part.endswith('>'):
                            next_url = url_part[1:-1]
                            if 'page_info=' in next_url:
                                # next_page_info = next_url.split('page_info=')[1].split('&')[0] # Not currently used
                                _logger.info(f"Extracted page_info token for next batch (but not using pagination yet)")
                                break

        except Exception as e:
            _logger.exception(f"Error fetching products from Shopify: {str(e)}")

        _logger.info(f"Fetched a total of {len(all_products)} products from Shopify.")
        return all_products

    @api.model
    def sync_products_to_odoo(self):
        """
        Sync Shopify products & inventory to Odoo.
        Fetches all products since last sync time using pagination.
        """
        ConfigParam = self.env['ir.config_parameter'].sudo()
        batch_size = int(ConfigParam.get_param('shopify.sync.product_batch_size', 50)) # Read here for logging
        _logger.info(f"Starting Shopify product sync (API fetch batch size: {batch_size})...")
        last_sync_time = ConfigParam.get_param('shopify.last_sync_time')
        # We'll set the new sync time at the very end based on the latest product processed

        # --- Get Location Mapping ---
        location_mapping = self._get_odoo_shopify_location_mapping()
        if not location_mapping:
             _logger.warning("Could not obtain Shopify location mapping. Inventory sync will be skipped for this run.")
        # --- End Location Mapping ---

        shopify_products = self.fetch_shopify_products(last_sync_time) # batch_size is now handled inside fetch_shopify_products

        if len(shopify_products) > 0:
            pass

        ProductTemplateModel = self.env['product.template']
        ProductModel = self.env['product.product']
        InventoryModel = self.env['stock.quant']
        AttributeModel = self.env['product.attribute']
        AttributeValueModel = self.env['product.attribute.value']
        TaxModel = self.env['account.tax']
        LocationModel = self.env['stock.location']
        ProductTmplAttrValueModel = self.env['product.template.attribute.value']
        # ConfigParam is already defined above
        variant_processing_batch_size = int(ConfigParam.get_param('shopify.sync.variant_processing_batch_size', 15)) # Default 15

        updated_templates, created_templates, skipped_variants = 0, 0, 0
        latest_update_time_processed = None # Track the latest timestamp processed successfully

        for product in shopify_products:
            shopify_product_id = str(product["id"])
            _logger.info(f"Processing Shopify Product ID: {shopify_product_id} Title: {product['title']}")

            if not product.get("variants"):
                _logger.warning(f"Skipping product {shopify_product_id} - No variants found.")
                continue

            first_variant = product["variants"][0]
            base_price = float(first_variant.get("price", 0.0))
            _logger.info(f"Using base price {base_price} from first variant (SKU: {first_variant.get('sku', 'N/A')}) for template {product['title']}")

            existing_template = ProductTemplateModel.search([('shopify_product_id', '=', shopify_product_id)], limit=1)
            if not existing_template:
                 _logger.warning(f"Searching for template by name as Shopify ID {shopify_product_id} not found: {product['title']}")
                 existing_template = ProductTemplateModel.search([('name', '=', product['title'])], limit=1)

            template_data = {
                'name': product['title'],
                'description_sale': product.get("body_html", ""),
                'type': 'product',
                'shopify_product_id': shopify_product_id,
                'list_price': base_price,
                'company_id': self.env.company.id,
                'vendor': product.get('vendor'),
            }

            main_image_url = (product.get("image", {}) or {}).get("src")
            if main_image_url:
                try:
                    image_response = requests.get(main_image_url, timeout=10)
                    if image_response.status_code == 200:
                        template_data['image_1920'] = base64.b64encode(image_response.content).decode('utf-8')
                    else:
                        _logger.warning(f"Failed to fetch main image for {product['title']}: Status code {image_response.status_code}")
                except Exception as e:
                    _logger.warning(f"Failed to fetch main image for {product['title']}: {str(e)}")

            # tax_lines = product.get('tax_lines', [])
            # if tax_lines:
            #     taxes = []
            #     for tax_line in tax_lines:
            #         tax_name = tax_line.get('title')
            #         tax_rate = tax_line.get('rate')
            #         if tax_name and tax_rate is not None:
            #             tax = TaxModel.search([
            #                 ('name', '=', tax_name), ('amount', '=', tax_rate * 100),
            #                 ('type_tax_use', '=', 'sale'), ('company_id', '=', self.env.company.id)
            #             ], limit=1)
            #             if not tax:
            #                 tax = TaxModel.create({
            #                     'name': tax_name, 'amount': tax_rate * 100, 'amount_type': 'percent',
            #                     'type_tax_use': 'sale', 'company_id': self.env.company.id,
            #                 })
            #             taxes.append(tax.id)
            #     if taxes:
            #         template_data['taxes_id'] = [(6, 0, taxes)]

            vendor_name = template_data.pop('vendor', False)
            if vendor_name:
                # Search for existing vendor
                vendor = self.env['res.partner'].search([
                    ('name', '=', vendor_name),
                    ('supplier_rank', '>', 0),
                    ('company_id', '=', self.env.company.id)
                ], limit=1)
                if not vendor:
                    # Create new vendor
                    vendor = self.env['res.partner'].create({
                        'name': vendor_name,
                        'supplier_rank': 1,
                        'company_id': self.env.company.id,
                    })
                    _logger.info(f"Created new vendor: {vendor_name}")
                template_data['seller_ids'] = [(4, vendor.id)]

            if existing_template:
                # Prevent changing the type of an existing product that might have transactions
                template_data.pop('type', None)
                existing_template.write(template_data)
                _logger.info(f"Updated product template: {existing_template.name} (Shopify ID: {shopify_product_id})")
                updated_templates += 1
            else:
                existing_template = ProductTemplateModel.create(template_data)
                _logger.info(f"Created new product template: {existing_template.name} (Shopify ID: {shopify_product_id})")
                created_templates += 1

            product_attribute_lines_cmds = []
            shopify_images = {img['id']: img['src'] for img in product.get('images', [])}
            _logger.debug(f"Shopify Images Map for product {shopify_product_id}: {shopify_images}") # LOGGING ADDED
            processed_variant_ids = set()
            variant_price_extra_cmds = {}

            # --- Batch Variant Processing ---
            all_variants = product.get("variants", [])
            _logger.info(f"Processing {len(all_variants)} variants for product {shopify_product_id} in batches of {variant_processing_batch_size}")

            # Fetch all inventory levels and costs for all variants of this product *before* processing batches
            all_variant_ids = [str(v['id']) for v in all_variants if v.get('id')]
            all_inventory_item_ids = [str(v['inventory_item_id']) for v in all_variants if v.get('inventory_item_id')]

            _logger.info(f"Fetching inventory levels for {len(all_inventory_item_ids)} inventory item IDs...")
            inventory_levels_by_item_id = self._fetch_shopify_inventory_levels(all_inventory_item_ids)
            _logger.info(f"Fetching costs for {len(all_inventory_item_ids)} inventory item IDs...")
            costs_by_item_id = self._fetch_shopify_inventory_item_costs_batch(all_inventory_item_ids)
            _logger.info("Finished pre-fetching inventory levels and costs for all variants.")


            for i in range(0, len(all_variants), variant_processing_batch_size):
                variant_batch = all_variants[i:i + variant_processing_batch_size]
                _logger.info(f"Processing variant batch {i // variant_processing_batch_size + 1}...")

                # Prepare attribute lines and values within the batch
                batch_attribute_values = {} # { 'Attribute Name': set('Value1', 'Value2') }
                for variant in variant_batch:
                    for option_index, option in enumerate(variant.get("option_values", [])): # Use option_values if available
                         option_name = (product.get("options", []) + [{}])[option_index].get("name") # Get corresponding option name
                         if option_name:
                             if option_name not in batch_attribute_values:
                                 batch_attribute_values[option_name] = set()
                             batch_attribute_values[option_name].add(option['value']) # Add the value
                    # Fallback to option1, option2, option3 if option_values not present
                    if not variant.get("option_values"):
                        for option_num in range(1, 4):
                            option_key = f"option{option_num}"
                            option_value = variant.get(option_key)
                            if option_value:
                                # Try to get the actual attribute name from product['options']
                                option_name = (product.get("options", []) + [{}, {}, {}])[option_num - 1].get("name")
                                if option_name: # Use the real name if found
                                    if option_name not in batch_attribute_values:
                                        batch_attribute_values[option_name] = set()
                                    batch_attribute_values[option_name].add(option_value)
                                # else: # Fallback if name not in product['options'] (less likely)
                                #     fallback_name = f"Option {option_num}"
                                #     if fallback_name not in batch_attribute_values:
                                #         batch_attribute_values[fallback_name] = set()
                                #     batch_attribute_values[fallback_name].add(option_value)


                # Ensure attributes and values exist in Odoo for this batch
                current_template_attribute_lines = existing_template.attribute_line_ids.mapped('attribute_id.name')
                new_attribute_line_cmds = []

                for attr_name, value_set in batch_attribute_values.items():
                    attribute = AttributeModel.search([('name', '=', attr_name)], limit=1)
                    if not attribute:
                        attribute = AttributeModel.create({'name': attr_name})
                        _logger.info(f"Created new product attribute: {attr_name}")

                    value_ids = []
                    for val_name in value_set:
                        attribute_value = AttributeValueModel.search([('name', '=', val_name), ('attribute_id', '=', attribute.id)], limit=1)
                        if not attribute_value:
                            attribute_value = AttributeValueModel.create({'name': val_name, 'attribute_id': attribute.id})
                            _logger.info(f"Created new attribute value: {val_name} for attribute {attr_name}")
                        value_ids.append(attribute_value.id)

                    # Check if this attribute line already exists on the template
                    existing_line = existing_template.attribute_line_ids.filtered(lambda line: line.attribute_id.id == attribute.id)
                    if not existing_line:
                         # Add command to create a new attribute line
                         new_attribute_line_cmds.append((0, 0, {'attribute_id': attribute.id, 'value_ids': [(6, 0, value_ids)]}))
                         _logger.info(f"Prepared command to add new attribute line '{attr_name}' with values: {value_set}")
                    else:
                         # Add command to update existing line's values (add missing ones)
                         # Get IDs already on the line
                         current_value_ids = set(existing_line.value_ids.ids)
                         # Find IDs from the batch that are not yet on the line
                         new_value_ids_to_add = [vid for vid in value_ids if vid not in current_value_ids]
                         if new_value_ids_to_add:
                              # Command to add values to existing line: (1, line_id, {'value_ids': [(4, value_id_to_add)]})
                              # Using (6, 0, ...) might be safer to replace all values if needed, but let's try adding first.
                              # Let's use (6, 0, ...) to ensure all required values are present.
                              all_required_values = list(current_value_ids.union(value_ids))
                              new_attribute_line_cmds.append((1, existing_line.id, {'value_ids': [(6, 0, all_required_values)]}))
                              _logger.info(f"Prepared command to update attribute line '{attr_name}' to ensure values: {value_set}")


                # Apply attribute line changes if any new commands were generated
                if new_attribute_line_cmds:
                    try:
                        _logger.info(f"Applying attribute line commands to template {existing_template.name}: {new_attribute_line_cmds}")
                        existing_template.write({'attribute_line_ids': new_attribute_line_cmds})
                        _logger.info(f"Successfully updated attribute lines for template {existing_template.name}.")
                    except Exception as attr_e:
                        _logger.error(f"Failed to write attribute lines for template {existing_template.name}: {attr_e}")
                        # Continue processing variants, but log the error
                        skipped_variants += len(variant_batch)
                        continue


                # --- Process Variants within the Batch ---
                for variant in variant_batch:
                    shopify_variant_id = str(variant["id"])
                    sku = variant.get("sku") or f"odoo-{shopify_product_id}-{shopify_variant_id}" # Fallback SKU
                    inventory_item_id_str = str(variant.get("inventory_item_id"))

                    # Find corresponding Odoo variant
                    # Search by Shopify Variant ID first, then by SKU
                    existing_variant = ProductModel.search([('shopify_variant_id', '=', shopify_variant_id)], limit=1)
                    if not existing_variant:
                         # Search by SKU only if not found by shopify_variant_id
                         # Ensure we search within the correct template
                         existing_variant = ProductModel.search([
                             ('default_code', '=', sku),
                             ('product_tmpl_id', '=', existing_template.id)
                         ], limit=1)
                         if not existing_variant and sku.startswith('odoo-'):
                              # Try searching by Odoo ID if using fallback SKU and not found by default_code
                              try:
                                   odoo_variant_id = int(sku.split('-')[-1])
                                   existing_variant = ProductModel.search([
                                       ('id', '=', odoo_variant_id),
                                       ('product_tmpl_id', '=', existing_template.id)
                                   ], limit=1)
                              except (ValueError, IndexError):
                                   pass # Invalid fallback SKU format

                    # --- Prepare Variant Data ---
                    variant_price = float(variant.get("price", 0.0))
                    variant_compare_at_price = float(variant.get("compare_at_price", 0.0) or 0.0) # Handle null
                    variant_weight = _convert_weight_to_kg(float(variant.get("grams", 0.0) or 0.0), 'g') # Shopify uses grams
                    variant_cost = costs_by_item_id.get(inventory_item_id_str, 0.0) # Get pre-fetched cost

                    variant_data = {
                        'default_code': sku,
                        'barcode': variant.get("barcode"),
                        'shopify_variant_id': shopify_variant_id,
                        'shopify_inventory_item_id': inventory_item_id_str,
                        'standard_price': variant_cost, # Map Shopify cost to Odoo cost
                        'weight': variant_weight,
                        'active': True, # Assume variants from Shopify are active
                        'product_tmpl_id': existing_template.id, # Ensure linked to template
                        # Price is handled via pricelist or template attribute value price extra
                        'tracking': 'lot' if variant.get('inventory_management') else 'none',
                    }

                    # --- Determine Attribute Values for this Variant ---
                    variant_attribute_values = []
                    # Use option_values if available
                    if variant.get("option_values"):
                         for option in variant.get("option_values", []):
                              attr_name = option.get('option_name') # Get name from option_values
                              val_name = option.get('value')
                              if attr_name and val_name:
                                   # Find the corresponding product.template.attribute.value
                                   # Search within the template's attribute lines for efficiency
                                   ptav_domain = [
                                       ('product_tmpl_id', '=', existing_template.id),
                                       ('attribute_id.name', '=', attr_name),
                                       ('product_attribute_value_id.name', '=', val_name)
                                   ]
                                   ptav = ProductTmplAttrValueModel.search(ptav_domain, limit=1)
                                   if ptav:
                                       variant_attribute_values.append(ptav.id)
                                   else:
                                        _logger.warning(f"Could not find Odoo ProductTmplAttrValue for Variant {sku}, Attr: '{attr_name}', Value: '{val_name}'. Domain: {ptav_domain}")
                    else: # Fallback to option1, option2, option3
                        for option_num in range(1, 4):
                            option_key = f"option{option_num}"
                            val_name = variant.get(option_key)
                            if val_name:
                                # Find the corresponding attribute name from product options
                                attr_name = (product.get("options", []) + [{}, {}, {}])[option_num - 1].get("name")
                                if attr_name:
                                    # Find the corresponding product.template.attribute.value
                                    ptav_domain = [
                                        ('product_tmpl_id', '=', existing_template.id),
                                        ('attribute_id.name', '=', attr_name),
                                        ('product_attribute_value_id.name', '=', val_name)
                                    ]
                                    ptav = ProductTmplAttrValueModel.search(ptav_domain, limit=1)
                                    if ptav:
                                        variant_attribute_values.append(ptav.id)
                                    else:
                                         _logger.warning(f"Could not find Odoo ProductTmplAttrValue for Variant {sku}, Attr: '{attr_name}', Value: '{val_name}' (using fallback {option_key}). Domain: {ptav_domain}")
                                else:
                                     _logger.warning(f"Could not determine attribute name for {option_key}='{val_name}' on variant {sku}.")


                    if not variant_attribute_values and len(existing_template.attribute_line_ids) > 0:
                         _logger.warning(f"Variant {sku} has no attribute values identified, but template has attributes. Skipping attribute linking.")
                    elif variant_attribute_values:
                         # Use the format (6, 0, [IDs]) for linking Many2many
                         variant_data['product_template_attribute_value_ids'] = [(6, 0, variant_attribute_values)]


                    # --- Create or Update Odoo Variant ---
                    if not existing_variant:
                        # Check for existing combination with the same attributes
                        combination_domain = [
                            ('product_tmpl_id', '=', existing_template.id),
                            ('product_template_attribute_value_ids', 'in', variant_attribute_values)
                        ]
                        
                        # Search for variants with the exact same attribute combination
                        combination_variant = ProductModel.search(combination_domain, limit=1)
                        
                        if combination_variant:
                            _logger.warning(f"Variant with combination already exists (ID: {combination_variant.id}). Updating Shopify IDs instead of creating new variant.")
                            
                            # Update the existing variant with Shopify IDs if missing
                            update_vals = {
                                'shopify_variant_id': shopify_variant_id,
                                'shopify_inventory_item_id': inventory_item_id_str,
                                'default_code': sku,  # Update SKU if different
                                'barcode': variant.get("barcode"),  # Update barcode if different
                            }
                            
                            # Only update fields that have values and are different
                            update_vals = {k: v for k, v in update_vals.items() if v and (
                                not getattr(combination_variant, k) or 
                                getattr(combination_variant, k) != v
                            )}
                            
                            if update_vals:
                                try:
                                    combination_variant.write(update_vals)
                                    _logger.info(f"Updated existing variant ID {combination_variant.id} with Shopify data")
                                except Exception as update_e:
                                    _logger.error(f"Failed to update existing variant: {update_e}")
                            
                            existing_variant = combination_variant
                        else:
                            # Create new variant only if combination doesn't exist
                            try:
                                existing_variant = ProductModel.create(variant_data)
                                _logger.info(f"Created new variant: {sku} (ID: {existing_variant.id})")
                            except Exception as create_e:
                                _logger.error(f"Failed to create variant {sku}: {create_e}")
                                skipped_variants += 1
                                continue
                    else:
                        # --- Update Existing Variant (Found by ID or SKU initially) ---
                        try:
                            # Ensure we don't unset attributes if they weren't resolved correctly
                            if 'product_template_attribute_value_ids' not in variant_data and len(existing_variant.product_template_attribute_value_ids) > 0:
                                 _logger.warning(f"Resolved attribute values for existing variant {sku} are empty, but variant already has values. Removing attribute data from update dict to prevent unlinking.")
                                 variant_data.pop('product_template_attribute_value_ids', None)

                            existing_variant.write(variant_data)
                            _logger.info(f"  Updated product variant: SKU {sku} (Odoo ID: {existing_variant.id}, Shopify Variant ID: {shopify_variant_id})")
                        except Exception as update_e:
                            _logger.error(f"  Failed to update product variant SKU {sku} (Odoo ID: {existing_variant.id}): {update_e}. Data: {variant_data}")
                            skipped_variants += 1
                            continue # Skip inventory update if update failed

                    # --- Update Price Extra on Template Attribute Value ---
                    # Calculate price difference from template's base price
                    price_extra = variant_price - base_price
                    # Apply price extra to the specific combination of attribute values for this variant
                    if variant_attribute_values:
                         # Find the specific ptav records matching this variant's combination
                         # This assumes variant_attribute_values contains the correct IDs of ProductTmplAttrValueModel records
                         ptav_records = ProductTmplAttrValueModel.browse(variant_attribute_values)
                         for ptav in ptav_records:
                              # Store command to apply later, avoiding multiple writes
                              # We only store the *last* price extra found for a given value combination in this batch
                              # This might need refinement if multiple variants share *some* but not all values.
                              # A safer approach might be to store price extra directly on product.product if Odoo version supports it well,
                              # or handle it via pricelist rules based on attributes.
                              # For now, let's update the ptav price_extra.
                              if ptav.id not in variant_price_extra_cmds or variant_price_extra_cmds[ptav.id] != price_extra:
                                   variant_price_extra_cmds[ptav.id] = price_extra # Store intended price_extra

                    # --- Update Inventory (Fixed to prevent doubling) ---
                    if location_mapping and inventory_item_id_str in inventory_levels_by_item_id:
                        inventory_levels = inventory_levels_by_item_id[inventory_item_id_str]
                        _logger.info(f"  Processing {len(inventory_levels)} inventory level records for Inv Item ID {inventory_item_id_str} (SKU: {sku})")

                        for level in inventory_levels:
                            shopify_location_id = str(level.get("location_id"))
                            available_qty = int(level.get("available", 0)) # Shopify 'available'

                            odoo_location_id = location_mapping.get(shopify_location_id)

                            if odoo_location_id:
                                try:
                                    # Get current quantity in Odoo
                                    current_quant = InventoryModel.sudo().search([
                                        ('product_id', '=', existing_variant.id),
                                        ('location_id', '=', odoo_location_id),
                                    ], limit=1)
                                    
                                    if current_quant:
                                        current_qty = current_quant.quantity
                                        # Calculate difference needed
                                        difference = available_qty - current_qty
                                        
                                        if difference != 0:
                                            _logger.info(f"  Current quantity: {current_qty}, Shopify quantity: {available_qty}, Applying difference: {difference}")
                                            InventoryModel.with_context(inventory_mode=True).sudo()._update_available_quantity(
                                                existing_variant,
                                                LocationModel.browse(odoo_location_id),
                                                difference  # Apply just the difference
                                            )
                                            _logger.info(f"    Successfully adjusted inventory for SKU {sku} at Odoo Loc {odoo_location_id} by {difference} (new qty: {available_qty}).")
                                        else:
                                            _logger.debug(f"    No inventory adjustment needed for SKU {sku} at Odoo Loc {odoo_location_id} (qty matches Shopify).")
                                    else:
                                        # No existing quant, create new one with exact quantity
                                        _logger.info(f"    No existing quant found for SKU {sku} at Odoo Loc {odoo_location_id}, creating with qty {available_qty}")
                                        InventoryModel.with_context(inventory_mode=True).sudo()._update_available_quantity(
                                            existing_variant,
                                            LocationModel.browse(odoo_location_id),
                                            available_qty
                                        )

                                except Exception as inv_e:
                                    _logger.error(f"    Failed to update inventory for SKU {sku} at Odoo Loc {odoo_location_id}: {inv_e}")
                                    # Attempt manual quant creation/update as fallback (less robust)
                                    try:
                                        _logger.warning(f"    Falling back to manual quant adjustment for SKU {sku} at Odoo Loc {odoo_location_id}.")
                                        quant = InventoryModel.sudo().search([
                                            ('product_id', '=', existing_variant.id),
                                            ('location_id', '=', odoo_location_id),
                                        ], limit=1)
                                        if quant:
                                            current_qty = quant.quantity
                                            difference = available_qty - current_qty
                                            if difference != 0:
                                                quant.sudo().write({'quantity': available_qty})
                                                _logger.info(f"      Manually updated existing quant for SKU {sku} at Loc {odoo_location_id} to {available_qty}.")
                                        else:
                                            quant = InventoryModel.sudo().create({
                                                'product_id': existing_variant.id,
                                                'location_id': odoo_location_id,
                                                'quantity': available_qty,
                                            })
                                            _logger.info(f"      Manually created new quant for SKU {sku} at Loc {odoo_location_id} with qty {available_qty}.")
                                    except Exception as manual_inv_e:
                                         _logger.error(f"      Manual quant adjustment also failed for SKU {sku} at Odoo Loc {odoo_location_id}: {manual_inv_e}")

                            else:
                                _logger.warning(f"  No Odoo location mapping found for Shopify Location ID: {shopify_location_id}. Cannot update inventory for SKU {sku} at this location.")
                    elif not location_mapping:
                         _logger.debug(f"  Skipping inventory update for SKU {sku} because location mapping is missing.")
                    elif inventory_item_id_str not in inventory_levels_by_item_id:
                         _logger.debug(f"  Skipping inventory update for SKU {sku} because no inventory levels were fetched for Item ID {inventory_item_id_str}.")


                    # Track latest processed update time
                    variant_updated_at = parser.parse(variant["updated_at"])
                    if latest_update_time_processed is None or variant_updated_at > latest_update_time_processed:
                        latest_update_time_processed = variant_updated_at

            # --- Apply Price Extra Updates After Batch ---
            if variant_price_extra_cmds:
                 _logger.info(f"Applying {len(variant_price_extra_cmds)} price extra updates for template {existing_template.name}...")
                 for ptav_id, price_extra in variant_price_extra_cmds.items():
                      try:
                           ptav_record = ProductTmplAttrValueModel.browse(ptav_id)
                           if ptav_record.exists() and ptav_record.price_extra != price_extra:
                                ptav_record.sudo().write({'price_extra': price_extra})
                                _logger.debug(f"  Set price_extra={price_extra} for PTAV ID {ptav_id} ({ptav_record.display_name})")
                      except Exception as pe_e:
                           _logger.error(f"  Failed to write price_extra for PTAV ID {ptav_id}: {pe_e}")


            # --- Final check for product update time ---
            product_updated_at = parser.parse(product["updated_at"])
            if latest_update_time_processed is None or product_updated_at > latest_update_time_processed:
                latest_update_time_processed = product_updated_at


        # --- Update Last Sync Time ---
        if latest_update_time_processed:
            # Add a small buffer to avoid precision issues? Maybe not necessary.
            new_sync_time_str = latest_update_time_processed.strftime('%Y-%m-%d %H:%M:%S')
            ConfigParam.set_param('shopify.last_sync_time', new_sync_time_str)
            _logger.info(f"Shopify sync completed. Updated last sync time to: {new_sync_time_str}")
        else:
            # If no products were processed, update time to now to prevent re-fetching same batch
            fallback_sync_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ConfigParam.set_param('shopify.last_sync_time', fallback_sync_time)
            _logger.info(f"Shopify sync completed. No new product updates found. Setting last sync time to current time: {fallback_sync_time}")


        _logger.info(f"Sync finished. Created: {created_templates}, Updated: {updated_templates}, Skipped Variants: {skipped_variants}")
        return True # Indicate success


# --- Model Extensions ---

class ProductTemplate(models.Model):
    _inherit = "product.template"

    shopify_product_id = fields.Char("Shopify Product ID", index=True, copy=False)
    # Add other Shopify-specific fields if needed

class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_variant_id = fields.Char("Shopify Variant ID", index=True, copy=False)
    shopify_inventory_item_id = fields.Char("Shopify Inventory Item ID", index=True, copy=False)
    # Inherit shopify_product_id from template for easier searching?
    shopify_product_id = fields.Char(
        related='product_tmpl_id.shopify_product_id',
        string="Shopify Product ID (Template)",
        store=False, # No need to store if related
        readonly=True
    )

class StockLocation(models.Model):
    _inherit = "stock.location"

    # No shopify_location_id needed here if mapping is done via warehouse
    # shopify_location_id = fields.Char("Shopify Location ID", index=True, copy=False)

class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_location_id = fields.Char("Shopify Location ID", index=True, copy=False, help="The ID of the corresponding Location in Shopify")
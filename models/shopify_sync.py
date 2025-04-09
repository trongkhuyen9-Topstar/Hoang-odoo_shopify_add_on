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

            if existing_template:
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

            # --- Batch Inventory Fetch ---
            all_variant_inventory_item_ids = [
                v.get("inventory_item_id") for v in product.get("variants", []) if v.get("inventory_item_id")
            ]
            _logger.info(f"Found {len(all_variant_inventory_item_ids)} inventory item IDs for product {shopify_product_id}.")
            inventory_levels_by_item_id = {}
            if all_variant_inventory_item_ids:
                inventory_levels_by_item_id = self._fetch_shopify_inventory_levels(all_variant_inventory_item_ids)
                _logger.info(f"Fetched inventory levels for {len(inventory_levels_by_item_id)} item IDs.")
            # --- End Batch Inventory Fetch ---

            # --- Batch Cost Fetch (already done above) ---
            # costs_by_item_id is available here

            # --- Prepare for Variant Processing ---
            all_variants = product.get("variants", [])
            shopify_images = {img['id']: img['src'] for img in product.get('images', [])}
            _logger.debug(f"Shopify Images Map for product {shopify_product_id}: {shopify_images}")
            processed_variant_ids_for_product = set() # Track processed variants for this product
            # Accumulate attribute/price commands across batches for this product
            accumulated_attr_line_cmds = []
            accumulated_price_extra_cmds = {} # Maps shopify_variant_id -> {value_id: price_extra}

            # --- Batch Inventory Fetch --- # Moved slightly earlier for clarity
            all_variant_inventory_item_ids = [
                v.get("inventory_item_id") for v in all_variants if v.get("inventory_item_id")
            ]
            _logger.info(f"Found {len(all_variant_inventory_item_ids)} inventory item IDs for product {shopify_product_id}.")
            inventory_levels_by_item_id = {}
            if all_variant_inventory_item_ids:
                inventory_levels_by_item_id = self._fetch_shopify_inventory_levels(all_variant_inventory_item_ids)
                _logger.info(f"Fetched inventory levels for {len(inventory_levels_by_item_id)} item IDs.")
            # --- End Batch Inventory Fetch ---

            # --- Batch Cost Fetch ---
            all_variant_inventory_item_ids_for_cost = [
                v.get("inventory_item_id") for v in all_variants if v.get("inventory_item_id")
            ]
            costs_by_item_id = {}
            if all_variant_inventory_item_ids_for_cost:
                 _logger.info(f"Fetching costs for {len(all_variant_inventory_item_ids_for_cost)} inventory item IDs for product {shopify_product_id}.")
                 costs_by_item_id = self._fetch_shopify_inventory_item_costs_batch(all_variant_inventory_item_ids_for_cost)
                 _logger.info(f"Fetched costs for {len(costs_by_item_id)} item IDs.")
            else:
                 _logger.info(f"No inventory item IDs found for product {shopify_product_id} to fetch costs.")
            # --- End Batch Cost Fetch ---
            # --- Variant Batch Processing Loop ---
            _logger.info(f"Processing {len(all_variants)} variants for product {shopify_product_id} in batches of {variant_processing_batch_size}...")
            for i in range(0, len(all_variants), variant_processing_batch_size):
                variant_batch = all_variants[i:i + variant_processing_batch_size]
                _logger.info(f"  Processing variant batch {i // variant_processing_batch_size + 1} ({len(variant_batch)} variants)")

                for variant in variant_batch:
                    # Existing variant processing logic starts here...
                    shopify_variant_id = str(variant["id"])
                    sku = variant.get("sku", "")
                    barcode = variant.get("barcode", "")
                    variant_price = float(variant.get("price", 0.0))
                    variant_weight_value = float(variant.get("weight", 0.0)) # Store original value
                    variant_weight_unit = variant.get("weight_unit", "kg") # Get unit, default kg
                    inventory_quantity = variant.get("inventory_quantity", 0) # Note: Likely total, use inventory levels API
                    inventory_item_id = variant.get("inventory_item_id") # Fetch inventory item ID here
                    variant_image_id = variant.get("image_id")
                shopify_variant_id = str(variant["id"])
                sku = variant.get("sku", "")
                barcode = variant.get("barcode", "")
                variant_price = float(variant.get("price", 0.0))
                # variant_cost = float(variant.get("cost", 0.0)) # Cost is likely on InventoryItem
                variant_weight_value = float(variant.get("weight", 0.0)) # Store original value
                variant_weight_unit = variant.get("weight_unit", "kg") # Get unit, default kg
                inventory_quantity = variant.get("inventory_quantity", 0) # Note: Likely total, use inventory levels API
                inventory_item_id = variant.get("inventory_item_id") # Fetch inventory item ID here
                variant_image_id = variant.get("image_id")

                # --- Determine Cost (using pre-fetched batch data) ---
                actual_cost = 0.0
                if inventory_item_id:
                    actual_cost = costs_by_item_id.get(str(inventory_item_id), 0.0)
                    _logger.info(f"  Retrieved cost {actual_cost} from pre-fetched batch data for Item ID {inventory_item_id} (SKU: {sku}).")
                else:
                    _logger.warning(f"  Cannot determine cost for SKU {sku} as InventoryItem ID is missing.")
                # --- End Determine Cost ---
                if not sku and len(product.get("variants", [])) > 1:
                     _logger.warning(f"Skipping variant for product '{product['title']}' (Shopify ID: {shopify_product_id}) due to missing SKU in multi-variant product.")
                     skipped_variants += 1
                     continue
                elif not sku:
                     sku = f"{existing_template.name}-DEFAULT"
                     _logger.info(f"Variant for product '{product['title']}' has no SKU. Using generated SKU: {sku}")

                variant_attribute_value_ids = []
                current_variant_price_extra_cmds = {}

                current_attribute_lines = existing_template.attribute_line_ids
                current_attributes = {line.attribute_id.id for line in current_attribute_lines}

                for option in product.get("options", []):
                    option_name = option["name"]
                    attribute = AttributeModel.search([('name', '=', option_name)], limit=1)
                    if not attribute:
                        attribute = AttributeModel.create({'name': option_name})

                    value_name = variant.get(f"option{option['position']}")
                    if value_name:
                        attribute_value = AttributeValueModel.search([
                            ('attribute_id', '=', attribute.id), ('name', '=', value_name)
                        ], limit=1)
                        if not attribute_value:
                            attribute_value = AttributeValueModel.create({
                                'attribute_id': attribute.id, 'name': value_name
                            })
                        variant_attribute_value_ids.append(attribute_value.id)

                        price_extra = variant_price - base_price
                        current_variant_price_extra_cmds[attribute_value.id] = price_extra
                        _logger.info(f"  Attribute Value: {value_name} (ID: {attribute_value.id}), Calculated Price Extra: {price_extra}")

                        # --- Accumulate Attribute Line Commands ---
                        # Check if attribute needs to be added to template lines
                        if attribute.id not in current_attributes:
                             # Check if already planned in accumulated commands for this product
                             if attribute.id not in [cmd[2]['attribute_id'] for cmd in accumulated_attr_line_cmds if cmd[0] == 0]:
                                 accumulated_attr_line_cmds.append((0, 0, {
                                     'attribute_id': attribute.id,
                                     'value_ids': [(6, 0, [attribute_value.id])] # Start with this value
                                 }))
                                 current_attributes.add(attribute.id) # Mark as handled for this product
                             else:
                                 # Attribute is planned, ensure this value is added
                                 for cmd in accumulated_attr_line_cmds:
                                      if cmd[0] == 0 and cmd[2]['attribute_id'] == attribute.id:
                                          if attribute_value.id not in cmd[2]['value_ids'][0][2]:
                                               cmd[2]['value_ids'][0][2].append(attribute_value.id)
                                          break
                        else:
                             # Attribute exists on template, check if value needs to be added
                             existing_line = next((line for line in current_attribute_lines if line.attribute_id.id == attribute.id), None)
                             if existing_line and attribute_value.id not in existing_line.value_ids.ids:
                                  # Check if already planned in accumulated commands
                                  planned_update = False
                                  for cmd in accumulated_attr_line_cmds:
                                      if cmd[0] == 1 and cmd[1] == existing_line.id:
                                          if attribute_value.id not in cmd[2]['value_ids'][0][2]:
                                               cmd[2]['value_ids'][0][2].append(attribute_value.id)
                                          planned_update = True
                                          break
                                  if not planned_update:
                                       # If not planned, add a command to update the existing line
                                       # Use (6, 0, ...) to replace existing values + add the new one
                                       all_value_ids = existing_line.value_ids.ids + [attribute_value.id]
                                       accumulated_attr_line_cmds.append((1, existing_line.id, {'value_ids': [(6, 0, all_value_ids)] }))

                        # --- Accumulate Price Extra Commands ---
                        if shopify_variant_id not in accumulated_price_extra_cmds:
                            accumulated_price_extra_cmds[shopify_variant_id] = {}
                        accumulated_price_extra_cmds[shopify_variant_id][attribute_value.id] = price_extra

                # --- Variant Create/Update Logic (largely unchanged, uses 'existing_variant') ---

                existing_variant = ProductModel.search([('default_code', '=', sku)], limit=1)
                if not existing_variant and variant_attribute_value_ids:
                     ptav_domain = [
                         ('attribute_line_id', 'in', existing_template.attribute_line_ids.ids),
                         ('product_attribute_value_id', 'in', variant_attribute_value_ids)
                     ]
                     ptav_ids = ProductTmplAttrValueModel.search(ptav_domain).ids
                     if ptav_ids and len(ptav_ids) == len(variant_attribute_value_ids):
                         variant_domain = [('product_tmpl_id', '=', existing_template.id)]
                         for ptav_id in ptav_ids:
                             variant_domain.append(('product_template_attribute_value_ids', '=', ptav_id))
                         existing_variant = ProductModel.search(variant_domain, limit=1)

                # Check for barcode conflicts before assigning
                final_barcode_value = None # Default to no barcode
                if barcode: # Only check if we have a barcode from Shopify
                    ProductModel = self.env['product.product'] # Ensure ProductModel is defined here
                    domain = [('barcode', '=', barcode)]
                    if existing_variant:
                        # Exclude the current variant if it already exists
                        domain.append(('id', '!=', existing_variant.id))

                    conflicting_product = ProductModel.search(domain, limit=1)

                    if conflicting_product:
                        _logger.warning(f"Barcode '{barcode}' from Shopify for SKU '{sku}' already exists on product '{conflicting_product.display_name}' (ID: {conflicting_product.id}). Skipping barcode assignment for this variant.")
                        # final_barcode_value remains None
                    else:
                        # No conflict found, or the barcode belongs to the current variant
                        final_barcode_value = barcode

                variant_data = {
                    'product_tmpl_id': existing_template.id,
                    'default_code': sku,
                    'barcode': final_barcode_value, # Use the checked value
                    'standard_price': actual_cost, # Use cost fetched from InventoryItem
                    'weight': _convert_weight_to_kg(variant_weight_value, variant_weight_unit), # Use conversion helper
                    'active': True,
                }

                variant_image_url = shopify_images.get(variant_image_id)
                variant_image_b64 = None
                if variant_image_url:
                    _logger.info(f"Attempting to fetch image URL: {variant_image_url} for variant SKU {sku}")
                    try:
                        image_response = requests.get(variant_image_url, timeout=10)
                        if image_response.status_code == 200:
                            variant_image_b64 = base64.b64encode(image_response.content).decode('utf-8')
                            variant_data['image_1920'] = variant_image_b64
                            _logger.info(f"Successfully fetched image for variant SKU {sku}")
                        else:
                            _logger.warning(f"Failed to fetch image for variant SKU {sku}: Status code {image_response.status_code}")
                    except Exception as e:
                        _logger.warning(f"Failed to fetch image for variant SKU {sku}: {str(e)}")
                else:
                    _logger.info(f"No specific image ID found for variant SKU {sku}. It will inherit template image.")

                # --- Logging: Check data before write/create ---
                log_variant_data = variant_data.copy() # Avoid logging full image data
                image_info = f"Yes (length: {len(variant_image_b64)})" if variant_image_b64 else "No"
                log_variant_data.pop('image_1920', None) # Remove image data for cleaner log
                _logger.info(f"Variant Data for SKU {sku} (before write/create): {log_variant_data}") # LOGGING MODIFIED
                _logger.info(f"  -> Includes image_1920: {image_info}") # LOGGING MODIFIED

                if existing_variant:
                    _logger.debug(f"Writing variant data for existing variant {existing_variant.id} (SKU: {sku})") # LOGGING ADDED
                    # --- ADDED sudo() ---
                    existing_variant.sudo().write(variant_data)
                    # --- END sudo() ---
                    _logger.info(f"Updated variant: SKU {sku} for template {existing_template.name}")
                    processed_variant_ids_for_product.add(existing_variant.id)
                else:
                    # Search again
                    ptav_domain = [
                         ('attribute_line_id', 'in', existing_template.attribute_line_ids.ids),
                         ('product_attribute_value_id', 'in', variant_attribute_value_ids)
                     ]
                    ptav_ids = ProductTmplAttrValueModel.search(ptav_domain).ids
                    if ptav_ids and len(ptav_ids) == len(variant_attribute_value_ids):
                         variant_domain = [('product_tmpl_id', '=', existing_template.id)]
                         for ptav_id in ptav_ids:
                             variant_domain.append(('product_template_attribute_value_ids', '=', ptav_id))
                         existing_variant = ProductModel.search(variant_domain, limit=1)

                    if existing_variant:
                         _logger.debug(f"Writing variant data for existing variant {existing_variant.id} (found after template update, SKU: {sku})") # LOGGING ADDED
                         # --- ADDED sudo() ---
                         existing_variant.sudo().write(variant_data)
                         # --- END sudo() ---
                         _logger.info(f"Updated variant (found after template update): SKU {sku}")
                         processed_variant_ids_for_product.add(existing_variant.id)
                    else:
                         _logger.error(f"Could not find or create variant for SKU {sku} and template {existing_template.name}. Manual check needed.")
                         skipped_variants += 1
                         continue

                # --- Set Price Extra on Attribute Values ---
                price_extra_cmds_for_variant = variant_price_extra_cmds.get(shopify_variant_id, {})
                _logger.info(f"Attempting to set price_extra for variant SKU {sku}: {price_extra_cmds_for_variant}")
                for value_id, price_extra_val in price_extra_cmds_for_variant.items():
                    ptav_search_domain = [
                        ('product_tmpl_id', '=', existing_template.id),
                        ('product_attribute_value_id', '=', value_id)
                    ]
                    ptav_recs = ProductTmplAttrValueModel.search(ptav_search_domain)
                    _logger.debug(f"  Searching for PTAV with domain: {ptav_search_domain}") # LOGGING ADDED
                    if ptav_recs:
                        _logger.info(f"  Found {len(ptav_recs)} PTAV records (IDs: {ptav_recs.ids}) for value ID {value_id}. Writing price_extra: {price_extra_val}")
                        try:
                            ptav_recs.write({'price_extra': price_extra_val})
                            _logger.info(f"  Successfully set price_extra {price_extra_val} for value ID {value_id} on template {existing_template.name}")
                        except Exception as e_price:
                            _logger.error(f"  Error writing price_extra for value ID {value_id}: {e_price}")
                    else:
                        _logger.warning(f"  Could not find PTAV record for value ID {value_id} on template {existing_template.name} to set price_extra.")


                # --- Handle Inventory (Multi-Location) ---
                if location_mapping: # Only proceed if we have a location map
                    inventory_item_id = variant.get("inventory_item_id")
                    if inventory_item_id:
                        # Get pre-fetched levels for this variant's item ID
                        current_variant_levels = inventory_levels_by_item_id.get(str(inventory_item_id), [])

                        if current_variant_levels:
                            _logger.info(f"Processing {len(current_variant_levels)} pre-fetched inventory levels for SKU {sku} (Item ID: {inventory_item_id})")
                            updated_odoo_location_ids = set() # Keep track of locations updated for this variant

                            for level in current_variant_levels: # Use pre-fetched levels
                                shopify_loc_id_str = str(level.get('location_id'))
                                available_qty_raw = level.get('available')
                                try:
                                    # Odoo quantities are floats
                                    available_qty = float(available_qty_raw) if available_qty_raw is not None else 0.0
                                except (ValueError, TypeError):
                                    _logger.warning(f"    Could not convert Shopify available quantity '{available_qty_raw}' to float for Loc ID {shopify_loc_id_str}. Defaulting to 0.0.")
                                    available_qty = 0.0

                                # --- DEBUG LOGGING ---
                                _logger.info(f"  Processing Inv Level - Shopify Loc ID: {shopify_loc_id_str}, Available Qty: {available_qty}")
                                # --- END DEBUG LOGGING ---

                                odoo_location_id = location_mapping.get(shopify_loc_id_str)

                                if odoo_location_id:
                                    # --- DEBUG LOGGING ---
                                    _logger.info(f"    Mapped to Odoo Loc ID: {odoo_location_id}")
                                    # --- END DEBUG LOGGING ---
                                    updated_odoo_location_ids.add(odoo_location_id)
                                    try:
                                        _logger.info(f"  Attempting to set inventory for SKU {sku} (Product ID {existing_variant.id}) at Odoo Loc ID {odoo_location_id} to {available_qty} using _update_available_quantity")
                                        # Use _update_available_quantity, ensuring we pass recordsets
                                        location_record = self.env['stock.location'].browse(odoo_location_id)
                                        if not location_record.exists():
                                            _logger.error(f"    Could not find stock.location record for ID {odoo_location_id}. Skipping inventory update.")
                                            continue # Skip this level if location record not found

                                        # Ensure product_id is also a recordset (should be already, but safer)
                                        product_record = self.env['product.product'].browse(existing_variant.id)
                                        if not product_record.exists():
                                             _logger.error(f"    Could not find product.product record for ID {existing_variant.id}. Skipping inventory update.")
                                             continue

                                        # available_qty is now guaranteed to be a float (default 0.0)
                                        _logger.info(f"    Attempting to set inventory for product={product_record.id}, location={location_record.id}, qty={available_qty}")

                                        # Find or create the specific stock.quant record
                                        # We use sudo() here as inventory adjustments often require higher privileges
                                        quant = InventoryModel.sudo().search([
                                            ('product_id', '=', product_record.id),
                                            ('location_id', '=', location_record.id),
                                            # Add lot_id, package_id, owner_id checks here if they become relevant
                                        ], limit=1)

                                        if quant:
                                            # If quant exists, update inventory_quantity if different and apply
                                            if quant.inventory_quantity != available_qty:
                                                quant.inventory_quantity = available_qty
                                                quant.action_apply_inventory()
                                                _logger.info(f"    Applied inventory update (set to {available_qty}) for existing quant ID {quant.id}")
                                            else:
                                                _logger.info(f"    Skipped inventory update for quant ID {quant.id} - quantity already {available_qty}")
                                        else:
                                            # If quant doesn't exist, create it and apply inventory
                                            # Odoo typically removes zero quants, but creating then applying should handle it.
                                            quant = InventoryModel.sudo().create({
                                                'product_id': product_record.id,
                                                'location_id': location_record.id,
                                                'inventory_quantity': available_qty,
                                                # Add lot_id, package_id, owner_id here if relevant
                                            })
                                            quant.action_apply_inventory()
                                            _logger.info(f"    Created new quant ID {quant.id} and applied inventory (set to {available_qty})")
                                    except Exception as e_inv:
                                        _logger.exception(f"  Error applying inventory adjustment for SKU {sku} at Odoo Loc ID {odoo_location_id}: {e_inv}")
                                else:
                                    _logger.warning(f"  Could not find mapped Odoo location for Shopify Location ID {shopify_loc_id_str}. Skipping inventory update for this location.")

                        else:
                             _logger.warning(f"Could not fetch inventory levels for SKU {sku} (Item ID: {inventory_item_id}). Inventory sync skipped for this variant.")
                    else:
                        _logger.warning(f"Shopify variant data for SKU {sku} is missing 'inventory_item_id'. Inventory sync skipped.")
                else:
                     _logger.warning("Location mapping not available. Inventory sync skipped for variant SKU {sku}.")

                 # --- End of Inner Variant Batch Loop (for variant in variant_batch) ---
             # --- End of Outer Variant Batch Loop (for i in range...) ---
 
             # --- Apply Accumulated Attribute Lines (after all batches for the product) ---
            if accumulated_attr_line_cmds:
                  _logger.info(f"Applying {len(accumulated_attr_line_cmds)} accumulated attribute line commands for template: {existing_template.name}")
                  try:
                      # Use sudo() if permissions require it
                      existing_template.sudo().write({'attribute_line_ids': accumulated_attr_line_cmds})
                      # Refresh template to get updated attribute lines for price extra setting
                      existing_template.invalidate_cache(['attribute_line_ids'])
                      existing_template.refresh()
                      _logger.info(f"Successfully applied attribute lines for template {existing_template.name}")
                  except Exception as e_attr:
                       _logger.error(f"Error applying accumulated attribute lines for template {existing_template.name}: {e_attr}")
 
             # --- Apply Accumulated Price Extra (after attributes are set) ---
            _logger.info(f"Applying accumulated price extra commands for {len(accumulated_price_extra_cmds)} variants of product {existing_template.name}")
            for shopify_variant_id, price_extra_cmds_for_variant in accumulated_price_extra_cmds.items():
                  _logger.debug(f"  Processing price extra for Shopify Variant ID: {shopify_variant_id}")
                  for value_id, price_extra_val in price_extra_cmds_for_variant.items():
                      # Find the PTAV record *now* after attributes are potentially updated
                      ptav_search_domain = [
                          ('product_tmpl_id', '=', existing_template.id),
                          ('product_attribute_value_id', '=', value_id)
                      ]
                      # Use sudo() if needed
                      ptav_recs = ProductTmplAttrValueModel.sudo().search(ptav_search_domain)
                      _logger.debug(f"    Searching for PTAV with domain: {ptav_search_domain} to set price_extra={price_extra_val}")
                      if ptav_recs:
                          _logger.info(f"    Found {len(ptav_recs)} PTAV records (IDs: {ptav_recs.ids}) for value ID {value_id}. Writing price_extra: {price_extra_val}")
                          try:
                              # Use sudo() if needed
                              ptav_recs.sudo().write({'price_extra': price_extra_val})
                          except Exception as e_price:
                              _logger.error(f"    Error writing price_extra for value ID {value_id}: {e_price}")
                      else:
                          _logger.warning(f"    Could not find PTAV record for value ID {value_id} on template {existing_template.name} to set price_extra after batch processing.")
 
             # --- Optional: Deactivate Odoo variants not present in this sync for this product ---
             # This requires fetching all existing Odoo variants for the template first
             # existing_odoo_variants = ProductModel.sudo().search([('product_tmpl_id', '=', existing_template.id)])
             # variants_to_deactivate = existing_odoo_variants.filtered(lambda v: v.id not in processed_variant_ids_for_product)
             # if variants_to_deactivate:
             #    _logger.info(f"Deactivating {len(variants_to_deactivate)} Odoo variants for template {existing_template.name} not found in Shopify sync.")
             #    variants_to_deactivate.sudo().write({'active': False})
 
             # Track the latest update time processed successfully
            product_update_time = product.get('updated_at')
            if product_update_time:
                  try:
                      current_time_dt = parser.isoparse(product_update_time)
                      if latest_update_time_processed is None or current_time_dt > latest_update_time_processed:
                           latest_update_time_processed = current_time_dt
                  except ValueError:
                       _logger.warning(f"Could not parse 'updated_at' timestamp '{product_update_time}' for product {shopify_product_id}")
 
             # Commit after each product is fully processed (including all its variant batches)
             # Consider potential performance impact of committing frequently vs. risk of losing work on error
            try:
                self.env.cr.commit()
                _logger.info(f"Committed transaction after processing product {shopify_product_id}")
            except Exception as commit_e:
                  _logger.error(f"Error committing transaction after product {shopify_product_id}: {commit_e}. Rolling back.")
                  self.env.cr.rollback()
                  # Decide if you want to stop the whole sync or continue with the next product
                  # break # Example: Stop sync on commit error
 
             # --- End of 'for product in shopify_products:' loop ---

        # Update last_sync_time based on the last processed product's timestamp
        # Update last_sync_time based on the latest 'updated_at' timestamp encountered across ALL processed products
        # This requires tracking the maximum timestamp during the loop.
        # Let's refine this logic. We should ideally store the timestamp of the *last successfully processed* product
        # from the *current run* to ensure we don't miss updates if an error occurs mid-sync.
        # For now, we'll update it based on the last product processed in the loop if successful.
        # A more robust approach might involve storing progress per product.

        latest_update_time_in_batch = None
        if shopify_products:
            # Find the latest 'updated_at' from the fetched products (assuming they are sorted ascending)
            # Filter out products without 'updated_at' just in case
            valid_update_times = [p.get('updated_at') for p in shopify_products if p.get('updated_at')]
            if valid_update_times:
                latest_update_time_in_batch = max(valid_update_times)

        # Only update last_sync_time if the sync seemed successful and we have a new time
        # We might need a more robust success check later
        if latest_update_time_in_batch:
             # Add a small buffer (e.g., 1 second) to avoid potential clock skew issues if needed,
             # but using the exact time of the last processed item is usually fine.
             # new_last_sync_time = (parser.isoparse(latest_update_time_in_batch) + timedelta(seconds=1)).isoformat()
             new_last_sync_time = latest_update_time_in_batch
             ConfigParam.set_param('shopify.last_sync_time', new_last_sync_time)
             _logger.info(f"Set last sync time (shopify.last_sync_time) to: {new_last_sync_time}")
        elif shopify_products:
             _logger.warning("Processed products but could not determine the latest 'updated_at' timestamp. Last sync time not updated.")
        elif shopify_products:
             _logger.warning("Processed products but could not determine the 'updated_at' of the last one. Last sync time not updated.")
        else:
             _logger.info("No products processed in this batch. Last sync time not updated.")


        _logger.info(f"Batch sync completed: {created_templates} created templates, {updated_templates} updated templates, {skipped_variants} skipped variants.")
        return {
            "status": "success",
            "created": created_templates,
            "updated": updated_templates,
            "skipped": skipped_variants
        }


# Add to product.template
class ProductTemplate(models.Model):
    _inherit = "product.template"
    shopify_product_id = fields.Char("Shopify Product ID", index=True, copy=False)


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_variant_id = fields.Char("Shopify Variant ID", index=True) # Add if needed
    shopify_product_id = fields.Char(
        "Shopify Product ID",
        related='product_tmpl_id.shopify_product_id',
        store=True,
        index=True,
        help="Technical field to link variant to Shopify Product ID on template"
    )


# Add shopify_location_id to stock.location for mapping
class StockLocation(models.Model):
    _inherit = "stock.location"

    shopify_location_id = fields.Char("Shopify Location ID", index=True, copy=False, readonly=True,
                                     help="Technical field to link Odoo Stock Locations to Shopify Locations.")


# Add shopify_location_id to stock.warehouse for mapping
class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    shopify_location_id = fields.Char("Shopify Location ID", index=True, copy=False, readonly=True,
                                     help="Technical field to link Odoo Warehouses to Shopify Locations.")
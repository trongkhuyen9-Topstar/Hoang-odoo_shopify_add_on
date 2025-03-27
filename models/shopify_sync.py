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

        if not shopify_access_token or not shopify_store_url:
            _logger.error("Missing Shopify credentials. Ensure they are set in system parameters.")
            return []

        base_url = f"https://{shopify_store_url}/admin/api/2023-01/products.json"
        headers = {"X-Shopify-Access-Token": shopify_access_token}

        params = {
            "limit": batch_size,
            "order": "updated_at asc"
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
    def sync_products_to_odoo(self, batch_size=50):
        """
        Sync Shopify products & inventory to Odoo
        """
        _logger.info(f"Starting Shopify product sync (batch size: {batch_size})...")
        last_sync_time = self.env['ir.config_parameter'].sudo().get_param('shopify.last_sync_time')
        current_sync_time = datetime.now().isoformat()
        shopify_products = self.fetch_shopify_products(last_sync_time, batch_size)

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

        updated_templates, created_templates, skipped_variants = 0, 0, 0

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

            for variant in product.get("variants", []):
                shopify_variant_id = str(variant["id"])
                sku = variant.get("sku", "")
                barcode = variant.get("barcode", "")
                variant_price = float(variant.get("price", 0.0))
                variant_cost = float(variant.get("cost", 0.0))
                variant_weight = float(variant.get("weight", 0.0))
                inventory_quantity = variant.get("inventory_quantity", 0)
                variant_image_id = variant.get("image_id")

                _logger.info(f"Processing Variant - SKU: {sku}, Price: {variant_price}, Image ID: {variant_image_id}")

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

                        if attribute.id not in current_attributes:
                             if attribute.id not in [cmd[2]['attribute_id'] for cmd in product_attribute_lines_cmds if cmd[0] == 0]:
                                 product_attribute_lines_cmds.append((0, 0, {
                                     'attribute_id': attribute.id,
                                     'value_ids': [(6, 0, [attribute_value.id])]
                                 }))
                                 current_attributes.add(attribute.id)
                        else:
                             existing_line = next((line for line in current_attribute_lines if line.attribute_id.id == attribute.id), None)
                             if existing_line and attribute_value.id not in existing_line.value_ids.ids:
                                 product_attribute_lines_cmds.append((1, existing_line.id, {'value_ids': [(4, attribute_value.id)]}))

                variant_price_extra_cmds[shopify_variant_id] = current_variant_price_extra_cmds

                if product_attribute_lines_cmds:
                    _logger.info(f"Updating attribute lines for template: {existing_template.name}")
                    existing_template.write({'attribute_line_ids': product_attribute_lines_cmds})
                    product_attribute_lines_cmds = []

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

                variant_data = {
                    'product_tmpl_id': existing_template.id,
                    'default_code': sku,
                    'barcode': barcode if barcode else None,
                    'standard_price': variant_cost,
                    'weight': variant_weight,
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
                    existing_variant.write(variant_data)
                    _logger.info(f"Updated variant: SKU {sku} for template {existing_template.name}")
                    processed_variant_ids.add(existing_variant.id)
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
                         existing_variant.write(variant_data)
                         _logger.info(f"Updated variant (found after template update): SKU {sku}")
                         processed_variant_ids.add(existing_variant.id)
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


                # --- Handle Inventory ---
                shopify_location_name = 'Default Shopify Location' # Placeholder
                odoo_location = LocationModel.search([
                    ('name', '=', shopify_location_name), ('usage', '=', 'internal'),
                    ('company_id', '=', self.env.company.id)
                ], limit=1)
                if not odoo_location:
                    odoo_location = LocationModel.create({
                        'name': shopify_location_name, 'usage': 'internal', 'company_id': self.env.company.id,
                    })
                    _logger.info(f"Created Odoo stock location: {shopify_location_name}")

                try:
                    current_quant = InventoryModel.search([
                        ('product_id', '=', existing_variant.id), ('location_id', '=', odoo_location.id)
                    ])
                    if current_quant:
                         if current_quant.quantity != inventory_quantity:
                              current_quant.sudo().write({'inventory_quantity': inventory_quantity})
                              _logger.info(f"Updated inventory for {sku} at {odoo_location.name} to {inventory_quantity}")
                    elif inventory_quantity > 0:
                         InventoryModel.sudo().create({
                             'product_id': existing_variant.id, 'location_id': odoo_location.id,
                             'inventory_quantity': inventory_quantity,
                         })
                         _logger.info(f"Set initial inventory for {sku} at {odoo_location.name} to {inventory_quantity}")
                except Exception as e:
                    _logger.exception(f"Error updating inventory for variant {sku}: {str(e)}")

            # --- Optional: Deactivate Odoo variants not present in Shopify ---
            # ...


        if shopify_products:
            self.env['ir.config_parameter'].sudo().set_param('shopify.last_sync_time', current_sync_time)

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

    # shopify_variant_id = fields.Char("Shopify Variant ID", index=True) # Add if needed
    shopify_product_id = fields.Char(
        "Shopify Product ID",
        related='product_tmpl_id.shopify_product_id',
        store=True,
        index=True,
        help="Technical field to link variant to Shopify Product ID on template"
    )
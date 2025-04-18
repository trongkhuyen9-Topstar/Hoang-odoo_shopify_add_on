# -*- coding: utf-8 -*-
import logging
import requests
import json
import time
from odoo import models, fields, api, _

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
        """Test connection to Shopify"""
        try:
            headers = self._get_shopify_headers()
            url = f"{self._get_shopify_base_url()}/admin/api/2023-01/shop.json"
            response = requests.get(url, headers=headers, timeout=10)
            return response.status_code == 200
        except Exception as e:
            _logger.error(f"Connection test failed: {str(e)}")
            return False

    def _get_shopify_headers(self):
        """Get Shopify API headers"""
        return {
            "X-Shopify-Access-Token": self.env['ir.config_parameter'].sudo().get_param('shopify.access_token'),
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _get_shopify_base_url(self):
        """Get Shopify store URL"""
        store_url = self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')
        return f"https://{store_url}"

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
                If None, syncs templates modified since the last sync.
        """
        _logger.info("Starting Odoo to Shopify product sync...")

        # Verify Shopify credentials
        if not self._verify_shopify_credentials():
            return {
                "status": "error",
                "message": "Missing or invalid Shopify credentials"
            }

        headers = self._get_shopify_headers()
        base_url = self._get_shopify_base_url()

        # Record sync start time
        sync_start_time = fields.Datetime.now()

        # Get products to sync
        templates_to_sync = self._get_templates_to_sync(template_ids)
        if not templates_to_sync:
            self._update_last_sync_time(sync_start_time)
            return {"status": "success", "message": "No products needed syncing."}

        # Get location mapping
        location_mapping = self._get_valid_location_mapping(headers, base_url)
        if not location_mapping:
            _logger.warning("No valid location mappings found - inventory sync will be skipped")

        # Sync products
        results = {
            "created": 0,
            "updated": 0,
            "failed": 0,
            "errors": []
        }

        for template in templates_to_sync:
            self._sync_single_template(template, headers, base_url, location_mapping, results)

        # Update last sync time
        self._update_last_sync_time(sync_start_time)

        # Return results
        return self._prepare_sync_results(results)

    def _verify_shopify_credentials(self):
        """Verify Shopify credentials are configured"""
        if not all([
            self.env['ir.config_parameter'].sudo().get_param('shopify.access_token'),
            self.env['ir.config_parameter'].sudo().get_param('shopify.store_url')
        ]):
            _logger.error("Missing Shopify credentials")
            return False
        return True

    def _get_templates_to_sync(self, template_ids):
        """Get product templates to sync"""
        TemplateModel = self.env['product.template']
        
        if template_ids:
            return TemplateModel.browse(template_ids).filtered(lambda t: t.active)
        
        last_sync = self.env['ir.config_parameter'].sudo().get_param('shopify.last_export_time')
        domain = [('active', '=', True)]
        
        if last_sync:
            domain = ['|',
                    ('write_date', '>', last_sync),
                    ('product_variant_ids.write_date', '>', last_sync)
                    ('product_variant_ids.stock_move_ids.write_date', '>', last_sync)
                    ] + domain
        
        return TemplateModel.search(domain, limit=100)  # Increased limit from 1 to 100

    def _get_valid_location_mapping(self, headers, base_url):
        """Get and validate location mapping"""
        try:
            raw_mapping = self.env['shopify.sync'].sudo()._get_odoo_shopify_location_mapping()
            if not raw_mapping:
                return {}
                
            # Verify Shopify locations exist
            valid_shopify_locs = self._verify_shopify_locations(
                list(raw_mapping.keys()),
                headers,
                base_url
            )
            
            # Verify Odoo locations exist
            valid_mapping = {}
            for shopify_loc_id, odoo_loc_id in raw_mapping.items():
                if (str(shopify_loc_id) in valid_shopify_locs and 
                    self.env['stock.location'].browse(odoo_loc_id).exists()):
                    valid_mapping[str(shopify_loc_id)] = int(odoo_loc_id)
            
            return valid_mapping
        except Exception as e:
            _logger.error(f"Failed to get location mapping: {str(e)}")
            return {}

    def _verify_shopify_locations(self, location_ids, headers, base_url):
        """Verify Shopify locations exist"""
        valid_locations = []
        for loc_id in location_ids:
            try:
                url = f"{base_url}/admin/api/2023-01/locations/{loc_id}.json"
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    valid_locations.append(str(loc_id))
            except Exception as e:
                _logger.error(f"Error verifying location {loc_id}: {str(e)}")
        return valid_locations

    def _sync_single_template(self, template, headers, base_url, location_mapping, results):
        """Sync a single product template to Shopify"""
        try:
            # Prepare product data
            product_data = self._prepare_shopify_product_data(template)
            if not product_data:
                _logger.warning(f"Skipping template {template.name} - no active variants")
                return

            # Determine if creating or updating
            shopify_product_id = template.shopify_product_id
            if shopify_product_id and shopify_product_id.isdigit():
                result = self._update_shopify_product(
                    template,
                    int(shopify_product_id),
                    product_data,
                    headers,
                    base_url
                )
            else:
                result = self._create_shopify_product(
                    template,
                    product_data,
                    headers,
                    base_url
                )

            # Process result
            if result['status']:
                if result['action'] == 'create':
                    results['created'] += 1
                else:
                    results['updated'] += 1
                
                # Sync inventory if successful
                if location_mapping:
                    inventory_result = self._sync_inventory_levels_to_shopify(
                        template,
                        location_mapping,
                        headers,
                        base_url
                    )
                    if not inventory_result['status']:
                        results['errors'].extend(inventory_result['errors'])
            else:
                results['failed'] += 1
                results['errors'].append(result['error'])

        except Exception as e:
            error_msg = f"Unexpected error syncing template {template.name}: {str(e)}"
            _logger.error(error_msg, exc_info=True)
            results['failed'] += 1
            results['errors'].append(error_msg)

    def _update_shopify_product(self, template, shopify_id, product_data, headers, base_url):
        """Update existing product in Shopify"""
        try:
            url = f"{base_url}/admin/api/2023-01/products/{shopify_id}.json"
            response = requests.put(
                url,
                headers=headers,
                data=json.dumps({"product": product_data}),
                timeout=30
            )

            if response.status_code == 200:
                self._update_odoo_variant_ids(template, response.json().get("product", {}))
                return {'status': True, 'action': 'update'}
            
            error_msg = f"Update failed for {template.name}: {response.status_code} - {response.text}"
            _logger.error(error_msg)
            return {'status': False, 'error': error_msg}

        except Exception as e:
            error_msg = f"Update error for {template.name}: {str(e)}"
            _logger.error(error_msg, exc_info=True)
            return {'status': False, 'error': error_msg}

    def _create_shopify_product(self, template, product_data, headers, base_url):
        """Create new product in Shopify"""
        try:
            url = f"{base_url}/admin/api/2023-01/products.json"
            response = requests.post(
                url,
                headers=headers,
                data=json.dumps({"product": product_data}),
                timeout=30
            )

            if response.status_code == 201:
                product_resp = response.json().get("product", {})
                template.sudo().write({
                    "shopify_product_id": str(product_resp.get("id"))
                })
                self._update_odoo_variant_ids(template, product_resp)
                return {'status': True, 'action': 'create'}
            
            error_msg = f"Create failed for {template.name}: {response.status_code} - {response.text}"
            _logger.error(error_msg)
            return {'status': False, 'error': error_msg}

        except Exception as e:
            error_msg = f"Create error for {template.name}: {str(e)}"
            _logger.error(error_msg, exc_info=True)
            return {'status': False, 'error': error_msg}

    def _prepare_shopify_product_data(self, template):
        """Prepare product data for Shopify API"""
        active_variants = template.product_variant_ids.filtered(lambda v: v.active)
        if not active_variants:
            return None

        shopify_product_status = self.env['ir.config_parameter'].sudo().get_param(
            'shopify.product_status', 'active'
        )

        variants_data = []
        for variant in active_variants:
            variant_data = {
                "price": str(variant.list_price),
                "sku": variant.default_code or f"odoo-{template.id}-{variant.id}",
                "inventory_management": "shopify",
                "requires_shipping": True,
                "barcode": variant.barcode or "",
                "weight": variant.weight,
                "weight_unit": "kg",
            }

            if variant.shopify_variant_id and variant.shopify_variant_id.isdigit():
                variant_data["id"] = int(variant.shopify_variant_id)

            # Add option values
            option_values = variant.product_template_attribute_value_ids.mapped('name')
            for i in range(min(3, len(option_values))):
                variant_data[f'option{i+1}'] = option_values[i]

            variants_data.append(variant_data)

        product_data = {
            "title": template.name,
            "body_html": template.description_sale or template.name,
            "vendor": template.company_id.name if template.company_id else "Odoo",
            "product_type": template.categ_id.name if template.categ_id else "Uncategorized",
            "status": shopify_product_status,
            "variants": variants_data,
        }

        # Add options
        options = []
        for attr_line in template.attribute_line_ids[:3]:
            options.append({
                "name": attr_line.attribute_id.name,
                "values": list(attr_line.value_ids.mapped('name'))
            })
        if options:
            product_data["options"] = options

        if template.shopify_product_id and template.shopify_product_id.isdigit():
            product_data['id'] = int(template.shopify_product_id)

        return product_data

    def _update_odoo_variant_ids(self, template, shopify_product):
        """Update Odoo variant IDs from Shopify response"""
        shopify_variants = shopify_product.get('variants', [])
        if not shopify_variants:
            return

        variant_map = {
            (v.default_code or f"odoo-{template.id}-{v.id}"): v
            for v in template.product_variant_ids
        }

        for shopify_variant in shopify_variants:
            sku = shopify_variant.get('sku')
            if not sku or sku not in variant_map:
                continue

            update_vals = {}
            shopify_variant_id = str(shopify_variant.get('id'))
            inventory_item_id = str(shopify_variant.get('inventory_item_id'))

            if variant_map[sku].shopify_variant_id != shopify_variant_id:
                update_vals['shopify_variant_id'] = shopify_variant_id
            if variant_map[sku].shopify_inventory_item_id != inventory_item_id:
                update_vals['shopify_inventory_item_id'] = inventory_item_id

            if update_vals:
                variant_map[sku].sudo().write(update_vals)

    def _sync_inventory_levels_to_shopify(self, template, location_mapping, headers, base_url):
        """Sync inventory levels for all variants to Shopify"""
        _logger.info(f"Syncing inventory for template {template.name}")
        errors = []
        success = True

        odoo_to_shopify_loc = {
            odoo_loc_id: shopify_loc_id
            for shopify_loc_id, odoo_loc_id in location_mapping.items()
        }

        for variant in template.product_variant_ids.filtered(lambda v: v.active):

            _logger.info(f"Processing variant {variant.id} (SKU: {variant.default_code})")
            _logger.info(f"Inventory Item ID: {variant.shopify_inventory_item_id}")
            _logger.info(f"Stock in Odoo Location {odoo_loc_id}: {qty} units")

            if not variant.shopify_inventory_item_id or not variant.shopify_inventory_item_id.isdigit():
                error_msg = f"Missing inventory item ID for variant {variant.default_code}"
                _logger.warning(error_msg)
                errors.append(error_msg)
                success = False
                continue

            for odoo_loc_id, shopify_loc_id in odoo_to_shopify_loc.items():
                try:
                    qty = self._get_product_stock_for_location(variant, odoo_loc_id)
                    if qty < 0:
                        qty = 0

                    payload = {
                        "location_id": int(shopify_loc_id),
                        "inventory_item_id": int(variant.shopify_inventory_item_id),
                        "available": qty
                    }

                    response = requests.post(
                        f"{base_url}/admin/api/2023-01/inventory_levels/set.json",
                        headers=headers,
                        data=json.dumps(payload),
                        timeout=20
                    )

                    if response.status_code == 429:
                        time.sleep(int(response.headers.get('Retry-After', 5)))
                        response = requests.post(
                            f"{base_url}/admin/api/2023-01/inventory_levels/set.json",
                            headers=headers,
                            data=json.dumps(payload),
                            timeout=20
                        )

                    if response.status_code != 200:
                        error_msg = (
                            f"Inventory update failed for {variant.default_code} "
                            f"at location {shopify_loc_id}: {response.text}"
                        )
                        _logger.error(error_msg)
                        errors.append(error_msg)
                        success = False

                except Exception as e:
                    error_msg = f"Inventory sync error for {variant.default_code}: {str(e)}"
                    _logger.error(error_msg, exc_info=True)
                    errors.append(error_msg)
                    success = False

        return {'status': success, 'errors': errors}

    def _get_product_stock_for_location(self, product, location_id):
        """Get available quantity for product at specific location"""
        stock_data = self.env['stock.quant'].read_group(
            [('product_id', '=', product.id), ('location_id', '=', location_id)],
            ['quantity:sum'],
            []
        )
        return max(0, int(stock_data[0]['quantity'])) if stock_data and stock_data[0].get('quantity') else 0

    def _update_last_sync_time(self, sync_time):
        """Update last sync timestamp"""
        self.env['ir.config_parameter'].sudo().set_param(
            'shopify.last_export_time',
            sync_time
        )
        _logger.info(f"Updated last sync time to {sync_time}")

    def _prepare_sync_results(self, results):
        """Format sync results for return"""
        message = (
            f"Sync completed: {results['created']} created, "
            f"{results['updated']} updated, {results['failed']} failed"
        )
        
        if results['errors']:
            message += "\nErrors:\n" + "\n".join(results['errors'])
            return {
                "status": "error",
                "message": message,
                "created": results['created'],
                "updated": results['updated'],
                "failed": results['failed']
            }
        
        return {
            "status": "success",
            "message": message,
            "created": results['created'],
            "updated": results['updated'],
            "failed": results['failed']
        }


class ProductProductShopify(models.Model):
    _inherit = "product.product"

    shopify_product_id = fields.Char(
        "Shopify Product ID",
        related='product_tmpl_id.shopify_product_id',
        store=False,
        readonly=True
    )
    shopify_variant_id = fields.Char(
        "Shopify Variant ID",
        index=True,
        copy=False
    )
    shopify_inventory_item_id = fields.Char(
        "Shopify Inventory Item ID",
        index=True,
        copy=False
    )

    def sync_template_to_shopify(self):
        """Sync the parent template of this variant to Shopify"""
        templates = self.mapped('product_tmpl_id')
        if templates:
            return self.env['odoo.sync'].sudo().sync_products_to_sync(template_ids=templates.ids)
        return False


class ProductTemplateShopify(models.Model):
    _inherit = "product.template"

    shopify_product_id = fields.Char(
        "Shopify Product ID",
        index=True,
        copy=False
    )

    def sync_to_shopify(self):
        """Sync this product template to Shopify"""
        return self.env['odoo.sync'].sudo().sync_products_to_shopify(template_ids=self.ids)
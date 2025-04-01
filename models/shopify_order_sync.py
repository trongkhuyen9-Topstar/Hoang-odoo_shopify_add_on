# -*- coding: utf-8 -*-
import logging
import requests
import time
from datetime import datetime
from dateutil import parser # Added for parsing dates
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class ShopifyOrderStatus(models.Model):
    _name = "shopify.order.status"
    # Consider renaming the model later if it handles full order sync, e.g., "shopify.order.sync.log"
    _description = "Shopify Order Sync Log & Status" # Updated description
    _order = "last_sync_date desc, shopify_order_id desc"

    name = fields.Char("Name", compute='_compute_name', store=True)
    shopify_order_id = fields.Char("Shopify Order ID", required=True, index=True)
    shopify_order_number = fields.Char("Shopify Order Number") # For easier identification
    shopify_financial_status = fields.Char("Shopify Financial Status")
    shopify_fulfillment_status = fields.Char("Shopify Fulfillment Status")
    # Link to Odoo Sale Order if it exists (assumes sale.order has shopify_order_id)
    odoo_order_id = fields.Many2one(
        'sale.order',
        string="Odoo Sale Order",
        compute='_compute_odoo_order_id', # Keep compute for potential updates
        store=True, # Store the link
        readonly=True, # Make readonly as it's set by the sync
        help="The corresponding Odoo Sale Order created or updated by the sync."
    )
    last_sync_date = fields.Datetime("Last Sync Date", readonly=True)
    sync_message = fields.Text("Sync Message") # To store success/error messages per order
    raw_shopify_data = fields.Text("Raw Shopify Data") # Optional: for debugging

    _sql_constraints = [
        ('shopify_order_id_uniq', 'unique (shopify_order_id)', 'Shopify Order ID must be unique!')
    ]

    @api.depends('shopify_order_id', 'shopify_order_number')
    def _compute_name(self):
        for record in self:
            name = f"Shopify Order {record.shopify_order_number or record.shopify_order_id or 'N/A'}"
            record.name = name

    # This compute method might become less critical if we set odoo_order_id directly during sync,
    # but it can be useful for linking existing records if the sync fails initially.
    @api.depends('shopify_order_id')
    def _compute_odoo_order_id(self):
        SaleOrder = self.env['sale.order']
        if 'shopify_order_id' not in SaleOrder._fields:
             _logger.warning("Field 'shopify_order_id' not found on 'sale.order'. Cannot link Shopify Order Status.")
             for record in self:
                 record.odoo_order_id = False
             return

        shopify_ids = [rec.shopify_order_id for rec in self if rec.shopify_order_id]
        if not shopify_ids:
            for record in self:
                record.odoo_order_id = False
            return

        # Search in batch
        orders = SaleOrder.search([('shopify_order_id', 'in', shopify_ids)])
        order_map = {order.shopify_order_id: order.id for order in orders}

        for record in self:
            record.odoo_order_id = order_map.get(record.shopify_order_id, False)


    @api.model
    def _get_shopify_credentials(self):
        """Gets Shopify credentials from system parameters."""
        get_param = self.env['ir.config_parameter'].sudo().get_param
        access_token = get_param('shopify.access_token')
        store_url = get_param('shopify.store_url')
        api_version = get_param('shopify.api_version', '2024-01') # Default to a recent version

        if not access_token or not store_url:
            raise UserError(_("Shopify credentials (shopify.access_token, shopify.store_url) are not configured in system parameters."))

        return access_token, store_url, api_version

    @api.model
    def _fetch_shopify_orders_batch(self, access_token, store_url, api_version, params):
        """Fetches a single batch of orders from Shopify."""
        headers = {"X-Shopify-Access-Token": access_token}
        api_url = f"https://{store_url}/admin/api/{api_version}/orders.json"
        orders = []
        next_page_info = None

        try:
            _logger.info(f"Fetching Shopify orders with params: {params}")
            response = requests.get(api_url, headers=headers, params=params, timeout=30)

            if response.status_code == 429: # Rate limit
                retry_after = int(response.headers.get('Retry-After', 10))
                _logger.warning(f"Rate limit hit fetching orders. Waiting {retry_after} seconds.")
                time.sleep(retry_after)
                return self._fetch_shopify_orders_batch(access_token, store_url, api_version, params) # Retry

            response.raise_for_status() # Raise HTTPError for other bad responses (4xx, 5xx)

            data = response.json()
            orders = data.get("orders", [])
            _logger.info(f"Fetched {len(orders)} orders in this batch.")

            # Handle pagination (Link header)
            link_header = response.headers.get("Link", "")
            if link_header:
                links = requests.utils.parse_header_links(link_header)
                for link in links:
                    if link.get('rel') == 'next':
                        # Extract page_info from the next URL
                        next_url = link.get('url')
                        if next_url and 'page_info=' in next_url:
                            next_page_info = next_url.split('page_info=')[1].split('&')[0]
                            _logger.info(f"Found next page_info: {next_page_info}")
                        break

        except requests.exceptions.RequestException as e:
            _logger.error(f"Shopify API request error fetching orders: {e}")
            # Consider whether to raise UserError or just log
        except Exception as e:
            _logger.exception(f"Unexpected error fetching Shopify orders: {e}")

        return orders, next_page_info


    # --- Customer & Address Sync Helpers ---
    @api.model
    def _map_shopify_country_to_odoo(self, shopify_country_code):
        """Maps Shopify country code to Odoo res.country record."""
        if not shopify_country_code:
            return False
        # Search by code (case-insensitive)
        country = self.env['res.country'].search([('code', '=ilike', shopify_country_code)], limit=1)
        if not country:
            _logger.warning(f"Could not find Odoo country for Shopify code: {shopify_country_code}")
        return country.id

    @api.model
    def _map_shopify_state_to_odoo(self, shopify_state_code, country_id):
        """Maps Shopify state code to Odoo res.country.state record."""
        if not shopify_state_code or not country_id:
            return False
        # Search by code and country
        state = self.env['res.country.state'].search([
            ('code', '=ilike', shopify_state_code),
            ('country_id', '=', country_id)
        ], limit=1)
        if not state:
             _logger.warning(f"Could not find Odoo state for Shopify code: {shopify_state_code} in country ID: {country_id}")
        return state.id

    @api.model
    def _prepare_address_vals(self, shopify_address_data, partner_id, address_type):
        """Prepares Odoo address vals from Shopify address data."""
        if not shopify_address_data:
            return {}

        country_id = self._map_shopify_country_to_odoo(shopify_address_data.get('country_code'))
        state_id = self._map_shopify_state_to_odoo(shopify_address_data.get('province_code'), country_id)

        return {
            'type': address_type,
            'parent_id': partner_id, # Link to the main customer/company partner
            'name': shopify_address_data.get('name') or f"{shopify_address_data.get('first_name', '')} {shopify_address_data.get('last_name', '')}".strip(),
            'street': shopify_address_data.get('address1'),
            'street2': shopify_address_data.get('address2'),
            'city': shopify_address_data.get('city'),
            'state_id': state_id,
            'zip': shopify_address_data.get('zip'),
            'country_id': country_id,
            'phone': shopify_address_data.get('phone'),
            'company_name': shopify_address_data.get('company'), # Add company name if available
            # We don't have a direct shopify_address_id, matching will be based on type and parent
        }

    @api.model
    def _sync_customer_and_addresses(self, customer_data, billing_address_data, shipping_address_data):
        """
        Finds or creates an Odoo partner (res.partner) based on Shopify customer data.
        Also syncs billing and shipping addresses.

        Args:
            customer_data (dict): The 'customer' dictionary from the Shopify order. Can be None.
            billing_address_data (dict): The 'billing_address' dictionary. Can be None.
            shipping_address_data (dict): The 'shipping_address' dictionary. Can be None.

        Returns:
            tuple: (odoo_partner_id, odoo_billing_address_id, odoo_shipping_address_id)
                   Returns (False, False, False) if essential identification data is missing.
        """
        Partner = self.env['res.partner']
        partner = None
        odoo_partner_id = False
        billing_address_id = False
        shipping_address_id = False

        # --- Identify or Create Main Partner ---
        if customer_data and customer_data.get('id'):
            # Normal flow: Customer has an ID
            shopify_customer_id = str(customer_data['id'])
            email = customer_data.get('email')
            first_name = customer_data.get('first_name', '')
            last_name = customer_data.get('last_name', '')
            name = f"{first_name} {last_name}".strip() or email or f"Shopify Customer {shopify_customer_id}" # Fallback name

            # Search for existing partner by Shopify Customer ID first
            partner = Partner.search([('shopify_customer_id', '=', shopify_customer_id)], limit=1)

            if not partner and email:
                # If not found by Shopify ID, try by email (in case it was created manually before)
                partner = Partner.search([('email', '=ilike', email), ('shopify_customer_id', '=', False)], limit=1)
                if partner:
                    _logger.info(f"Found partner by email '{email}' (ID: {partner.id}). Linking Shopify ID {shopify_customer_id}.")
                    try:
                        partner.write({'shopify_customer_id': shopify_customer_id})
                    except Exception as e:
                         _logger.error(f"Failed to link Shopify ID {shopify_customer_id} to partner {partner.id}: {e}")
                         # Continue with the found partner anyway

            partner_vals = {
                'name': name,
                'email': email,
                'phone': customer_data.get('phone'),
                'shopify_customer_id': shopify_customer_id,
                'type': 'contact', # Default type
            }

            if partner:
                try:
                    partner.write(partner_vals)
                    _logger.info(f"Updated partner '{partner.name}' (ID: {partner.id}) from Shopify Customer ID {shopify_customer_id}.")
                except Exception as e:
                     _logger.error(f"Failed to update partner {partner.id} for Shopify Customer ID {shopify_customer_id}: {e}")
            else:
                try:
                    partner = Partner.create(partner_vals)
                    _logger.info(f"Created new partner '{partner.name}' (ID: {partner.id}) for Shopify Customer ID {shopify_customer_id}.")
                except Exception as e:
                    _logger.error(f"Failed to create partner for Shopify Customer ID {shopify_customer_id}: {e}")
                    return False, False, False # Stop if partner creation fails

        elif billing_address_data or shipping_address_data:
            # Handle orders with no customer ID (guest checkout?)
            # Use address info. Prioritize billing, then shipping.
            address_for_info = billing_address_data or shipping_address_data
            _logger.warning("Order received without customer ID. Trying to use address info.")

            name = f"{address_for_info.get('first_name','')} {address_for_info.get('last_name',' ')}".strip()
            # Email might still be in top-level customer even without ID, or sometimes in address? Check Shopify docs.
            email = (customer_data or {}).get('email') # Check top-level customer dict if it exists
            phone = address_for_info.get('phone')

            if not name and not email:
                 _logger.error("Cannot create guest customer: No name or email found in order/address data.")
                 return False, False, False # Cannot proceed without identifier

            # Search by email first if available
            if email:
                partner = Partner.search([('email', '=ilike', email)], limit=1)

            # If not found by email, try by name and maybe phone (less reliable)
            if not partner and name:
                 search_domain = [('name', '=', name)]
                 # Only add phone if searching by name didn't yield results?
                 # if phone:
                 #    search_domain.append(('phone', '=', phone))
                 partner = Partner.search(search_domain, limit=1)

            if not partner:
                partner_vals = {
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'type': 'contact', # Default type
                    # No shopify_customer_id here
                }
                try:
                    partner = Partner.create(partner_vals)
                    _logger.info(f"Created new guest partner '{name}' (ID: {partner.id}) based on address info.")
                except Exception as e:
                    _logger.error(f"Failed to create guest partner '{name}': {e}")
                    return False, False, False
            else:
                 _logger.info(f"Found existing partner '{partner.name}' (ID: {partner.id}) matching guest info.")
        else:
             _logger.error("Cannot sync customer: No customer data or address data found in order.")
             return False, False, False # Cannot proceed

        if not partner:
             _logger.error("Partner record not found or created. Cannot sync addresses.")
             return False, False, False

        odoo_partner_id = partner.id

        # --- Sync Addresses ---
        # Prepare address values
        billing_vals = self._prepare_address_vals(billing_address_data, partner.id, 'invoice')
        shipping_vals = self._prepare_address_vals(shipping_address_data, partner.id, 'delivery')

        # Find or create Billing Address (type='invoice')
        if billing_vals:
            existing_billing = Partner.search([
                ('parent_id', '=', partner.id),
                ('type', '=', 'invoice'),
            ], limit=1)

            if existing_billing:
                try:
                    existing_billing.write(billing_vals)
                    billing_address_id = existing_billing.id
                    _logger.info(f"Updated billing address (ID: {billing_address_id}) for partner {partner.id}")
                except Exception as e:
                     _logger.error(f"Failed to update billing address for partner {partner.id}: {e}")
            else:
                try:
                    billing_vals['parent_id'] = partner.id
                    new_billing = Partner.create(billing_vals)
                    billing_address_id = new_billing.id
                    _logger.info(f"Created new billing address (ID: {billing_address_id}) for partner {partner.id}")
                except Exception as e:
                     _logger.error(f"Failed to create billing address for partner {partner.id}: {e}")

        # Find or create Shipping Address (type='delivery')
        is_shipping_same_as_billing = False
        if billing_address_data and shipping_address_data and billing_address_id:
             billing_key = (billing_address_data.get('address1',''), billing_address_data.get('zip',''), billing_address_data.get('city',''), billing_address_data.get('country_code',''))
             shipping_key = (shipping_address_data.get('address1',''), shipping_address_data.get('zip',''), shipping_address_data.get('city',''), shipping_address_data.get('country_code',''))
             if billing_key == shipping_key and billing_key != ('','','',''):
                 is_shipping_same_as_billing = True
                 shipping_address_id = billing_address_id # Use the same address record
                 _logger.info(f"Shipping address is same as billing (ID: {billing_address_id}) for partner {partner.id}")

        if shipping_vals and not is_shipping_same_as_billing:
            existing_shipping = Partner.search([
                ('parent_id', '=', partner.id),
                ('type', '=', 'delivery'),
            ], limit=1)

            if existing_shipping:
                 try:
                    existing_shipping.write(shipping_vals)
                    shipping_address_id = existing_shipping.id
                    _logger.info(f"Updated shipping address (ID: {shipping_address_id}) for partner {partner.id}")
                 except Exception as e:
                     _logger.error(f"Failed to update shipping address for partner {partner.id}: {e}")
            else:
                try:
                     shipping_vals['parent_id'] = partner.id
                     new_shipping = Partner.create(shipping_vals)
                     shipping_address_id = new_shipping.id
                     _logger.info(f"Created new shipping address (ID: {shipping_address_id}) for partner {partner.id}")
                except Exception as e:
                     _logger.error(f"Failed to create shipping address for partner {partner.id}: {e}")

        # If only one address was synced, set the other type to the same ID if not already set
        if billing_address_id and not shipping_address_id:
            shipping_address_id = billing_address_id
        elif shipping_address_id and not billing_address_id:
             billing_address_id = shipping_address_id

        # Ensure the main partner has default invoice/shipping addresses if they were created
        if partner and not partner.child_ids:
             update_partner_defaults = {}
             if billing_address_id: update_partner_defaults['property_account_position_id'] = billing_address_id # Check field name
             if shipping_address_id: update_partner_defaults['property_delivery_carrier_id'] = shipping_address_id # Check field name
             # This part needs verification of correct field names for default addresses on res.partner
             # partner.write(update_partner_defaults)


        return partner.id, billing_address_id, shipping_address_id
    # --- End Customer & Address Sync Helpers ---


    @api.model
    def sync_orders(self, batch_size=50): # Renamed method
        """Fetches recent Shopify orders and updates/creates corresponding Odoo records."""
        _logger.info(f"Starting Shopify order sync (batch size: {batch_size})...")
        try:
            access_token, store_url, api_version = self._get_shopify_credentials()
        except UserError as e:
            _logger.error(e)
            return # Stop if credentials are missing

        get_param = self.env['ir.config_parameter'].sudo().get_param
        set_param = self.env['ir.config_parameter'].sudo().set_param
        last_sync_time_param = 'shopify.order.last_sync_time' # Use a distinct param key
        last_sync_time = get_param(last_sync_time_param)
        current_sync_start_time = datetime.now().isoformat()

        params = {
            "limit": batch_size,
            "order": "updated_at desc",
            "status": "any", # Fetch orders of any status
            # Fetch fields needed for customer, order, lines, addresses, taxes, shipping, discounts
            "fields": (
                "id,order_number,name,email,created_at,updated_at,"
                "financial_status,fulfillment_status,"
                "total_price,subtotal_price,total_tax,total_discounts,"
                "currency,"
                "customer," # Includes default_address, email, phone, first/last name, id
                "billing_address,"
                "shipping_address,"
                "line_items," # Includes variant_id, product_id, sku, quantity, price, total_discount, tax_lines, name, title
                "shipping_lines," # Includes price, title, code
                "discount_codes," # Includes code, amount, type
                "note" # Customer note
                )
        }

        if last_sync_time:
            _logger.info(f"Syncing orders updated since: {last_sync_time}")
            params["updated_at_min"] = last_sync_time
        else:
             _logger.info("No previous sync time found. Syncing recent orders.")


        total_processed = 0
        total_so_created = 0
        total_so_updated = 0 # Or skipped
        total_errors = 0
        last_processed_order_update_time = None

        # Fetch orders using the updated params with more fields
        # TODO: Implement pagination loop if needed
        orders_batch, _ = self._fetch_shopify_orders_batch(
            access_token, store_url, api_version, params
        )

        if not orders_batch:
            _logger.info("No new orders found to sync in this run.")
            # Update sync time to now if we had a previous time, to avoid re-querying
            if last_sync_time:
                 set_param(last_sync_time_param, current_sync_start_time)
                 _logger.info(f"Updated last sync time ({last_sync_time_param}) to {current_sync_start_time} as no new orders were found.")
            return

        SaleOrder = self.env['sale.order']
        ProductProduct = self.env['product.product']
        SyncStatus = self.env['shopify.order.status'] # Use self.env for the current model

        for order_data in orders_batch:
            shopify_id = str(order_data['id'])
            order_number = order_data.get('name', 'N/A') # Use 'name' for order number like #1001
            sync_status_record = None
            _logger.info(f"Processing Shopify Order ID: {shopify_id} (Number: {order_number})")

            try:
                # --- Get or Create Sync Status Record ---
                sync_status_record = SyncStatus.search([('shopify_order_id', '=', shopify_id)], limit=1)
                status_vals = {
                    'shopify_order_id': shopify_id,
                    'shopify_order_number': order_number,
                    'shopify_financial_status': order_data.get('financial_status'),
                    'shopify_fulfillment_status': order_data.get('fulfillment_status'),
                    'last_sync_date': datetime.now(),
                    'raw_shopify_data': str(order_data) # Store raw data for debugging
                }
                if not sync_status_record:
                    sync_status_record = SyncStatus.create(status_vals)
                else:
                    sync_status_record.write(status_vals)


                # --- 1. Sync Customer and Addresses ---
                customer_data = order_data.get('customer')
                billing_address_data = order_data.get('billing_address')
                shipping_address_data = order_data.get('shipping_address')

                partner_id, invoice_partner_id, delivery_partner_id = self._sync_customer_and_addresses(
                    customer_data, billing_address_data, shipping_address_data
                )

                if not partner_id:
                    raise UserError("Failed to sync customer/address details.")


                # --- 2. Find or Create Sale Order ---
                existing_order = SaleOrder.search([('shopify_order_id', '=', shopify_id)], limit=1)
                order_vals = {
                    'partner_id': partner_id,
                    'partner_invoice_id': invoice_partner_id or partner_id,
                    'partner_shipping_id': delivery_partner_id or partner_id,
                    'shopify_order_id': shopify_id,
                    'shopify_order_number': order_number,
                    'date_order': parser.parse(order_data['created_at']).strftime('%Y-%m-%d %H:%M:%S') if order_data.get('created_at') else fields.Datetime.now(),
                    # 'state': self._map_shopify_status_to_odoo_state(order_data), # TODO
                    'note': order_data.get('note'),
                    # TODO: Map currency, pricelist
                    # 'company_id': self.env.company.id, # Ensure company context is right
                }

                # --- 3. Prepare Order Lines ---
                order_line_cmds = []
                shopify_lines = order_data.get('line_items', [])
                line_errors = []

                for line in shopify_lines:
                    sku = line.get('sku')
                    variant_id = str(line.get('variant_id')) if line.get('variant_id') else None
                    product_id = None
                    line_name = line.get('title') or line.get('name') # Prefer title, fallback to name

                    # Find Odoo product
                    odoo_product = None
                    if sku:
                        odoo_product = ProductProduct.search([('default_code', '=', sku)], limit=1)
                    if not odoo_product and variant_id and 'shopify_variant_id' in ProductProduct._fields:
                         odoo_product = ProductProduct.search([('shopify_variant_id', '=', variant_id)], limit=1)

                    if not odoo_product:
                        msg = f"Line '{line_name}': Could not find matching Odoo product (SKU: {sku or 'N/A'}, VarID: {variant_id or 'N/A'}). Skipping line."
                        _logger.error(f"Order {shopify_id}: {msg}")
                        line_errors.append(msg)
                        continue # Skip this line

                    product_id = odoo_product.id
                    line_vals = {
                        'product_id': product_id,
                        'name': line_name, # Use combined name from Shopify
                        'product_uom_qty': line.get('quantity', 0),
                        'price_unit': float(line.get('price', 0.0)),
                        # Calculate discount %: (total_discount / (price * qty)) * 100
                        'discount': (float(line.get('total_discount', 0.0)) * 100) / (float(line.get('price', 1.0)) * float(line.get('quantity', 1))) if float(line.get('price', 0.0)) * float(line.get('quantity', 0)) > 0 else 0.0,
                        # TODO: Map taxes (line.get('tax_lines'))
                        # 'tax_id': [(6, 0, [tax_ids])],
                    }
                    order_line_cmds.append((0, 0, line_vals))

                # --- TODO: Add Shipping Lines ---
                # --- TODO: Add Discount Lines ---

                order_vals['order_line'] = order_line_cmds

                # --- 4. Create or Update Sale Order ---
                odoo_order = None
                if existing_order:
                    # Basic update: just log for now, maybe update status fields later
                    _logger.info(f"Shopify Order {shopify_id} already exists as Odoo Order {existing_order.name} (ID: {existing_order.id}). Skipping line updates.")
                    # existing_order.write({'state': ...}) # Example status update
                    odoo_order = existing_order
                    total_so_updated += 1
                else:
                    if not order_line_cmds and not line_errors: # Don't create order with no lines unless there were errors finding products
                         _logger.warning(f"Order {shopify_id}: No valid order lines found. Skipping Sale Order creation.")
                         raise UserError("No valid order lines could be processed.")
                    elif not order_line_cmds and line_errors:
                         _logger.error(f"Order {shopify_id}: No order lines could be added due to product matching errors. Skipping Sale Order creation.")
                         raise UserError(f"Order lines could not be added: {'; '.join(line_errors)}")

                    new_order = SaleOrder.create(order_vals)
                    _logger.info(f"Created Odoo Sale Order {new_order.name} (ID: {new_order.id}) for Shopify Order {shopify_id}.")
                    odoo_order = new_order
                    total_so_created += 1

                # --- Update Sync Status Record with SO link and success message ---
                success_msg = f"Successfully processed."
                if line_errors:
                    success_msg += f" Line errors: {'; '.join(line_errors)}"
                sync_status_record.write({
                    'odoo_order_id': odoo_order.id if odoo_order else False,
                    'sync_message': success_msg
                })

                total_processed += 1
                last_processed_order_update_time = order_data.get('updated_at')
                self.env.cr.commit() # Commit after each successful order

            except Exception as e:
                 total_errors += 1
                 error_msg = f"Failed to process Shopify Order {shopify_id}: {e}"
                 _logger.exception(error_msg) # Log full traceback
                 self.env.cr.rollback() # Rollback the failed order transaction
                 self.env.clear()
                 # Update status record with error
                 if sync_status_record:
                     try:
                         sync_status_record.write({'sync_message': error_msg, 'odoo_order_id': False})
                         self.env.cr.commit() # Commit status update even on error
                     except Exception as e_stat:
                          _logger.error(f"Failed to write error status for Shopify Order {shopify_id}: {e_stat}")
                          self.env.cr.rollback()
                          self.env.clear()
                 # Continue to the next order

        # --- Final Logging and Sync Time Update ---
        if last_processed_order_update_time:
            new_last_sync_time = last_processed_order_update_time
            set_param(last_sync_time_param, new_last_sync_time)
            _logger.info(f"Set last sync time ({last_sync_time_param}) to: {new_last_sync_time}")
        else:
             # If we processed some orders but all failed, we might not want to update the time?
             # Or update to current_sync_start_time to avoid retrying the failed ones immediately?
             # Let's update to current_sync_start_time if there were errors but no successes.
             if total_errors > 0 and total_processed == 0:
                  set_param(last_sync_time_param, current_sync_start_time)
                  _logger.warning(f"Sync finished with errors and no successful orders. Updated last sync time ({last_sync_time_param}) to {current_sync_start_time} to avoid immediate retry.")
             else:
                  _logger.info(f"Sync finished. No orders were successfully processed to update the last sync time.")


        _logger.info(f"Shopify order sync finished. Processed: {total_processed}, SO Created: {total_so_created}, SO Existing: {total_so_updated}, Errors: {total_errors}.")


    @api.model
    def _auto_sync_orders(self): # Renamed cron method
        """Method called by the cron job."""
        _logger.info("Running auto Shopify order sync via cron...")
        self.sync_orders() # Call the renamed main sync method
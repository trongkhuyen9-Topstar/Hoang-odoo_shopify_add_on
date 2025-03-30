# -*- coding: utf-8 -*-
import logging
import requests
import time
from datetime import datetime
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class ShopifyOrderStatus(models.Model):
    _name = "shopify.order.status"
    _description = "Shopify Order Status Sync"
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
        compute='_compute_odoo_order_id',
        store=True,
        help="The corresponding Odoo Sale Order, if found based on Shopify Order ID."
    )
    last_sync_date = fields.Datetime("Last Sync Date", readonly=True)
    raw_shopify_data = fields.Text("Raw Shopify Data") # Optional: for debugging

    _sql_constraints = [
        ('shopify_order_id_uniq', 'unique (shopify_order_id)', 'Shopify Order ID must be unique!')
    ]

    @api.depends('shopify_order_id', 'shopify_order_number')
    def _compute_name(self):
        for record in self:
            name = f"Shopify Order {record.shopify_order_number or record.shopify_order_id or 'N/A'}"
            record.name = name

    @api.depends('shopify_order_id')
    def _compute_odoo_order_id(self):
        SaleOrder = self.env['sale.order']
        # Check if 'shopify_order_id' exists on sale.order before searching
        if 'shopify_order_id' not in SaleOrder._fields:
             _logger.warning("Field 'shopify_order_id' not found on 'sale.order'. Cannot link Shopify Order Status.")
             for record in self:
                 record.odoo_order_id = False
             return

        for record in self:
            if record.shopify_order_id:
                order = SaleOrder.search([('shopify_order_id', '=', record.shopify_order_id)], limit=1)
                record.odoo_order_id = order.id
            else:
                record.odoo_order_id = False

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


    @api.model
    def sync_order_statuses(self, batch_size=50):
        """Fetches recent Shopify orders and updates their status in Odoo."""
        _logger.info("Starting Shopify order status sync...")
        try:
            access_token, store_url, api_version = self._get_shopify_credentials()
        except UserError as e:
            _logger.error(e)
            return # Stop if credentials are missing

        get_param = self.env['ir.config_parameter'].sudo().get_param
        set_param = self.env['ir.config_parameter'].sudo().set_param
        last_sync_time_param = 'shopify.order_status.last_sync_time'
        last_sync_time = get_param(last_sync_time_param)
        # Use current time before fetching starts as the marker for the next sync
        current_sync_start_time = datetime.now().isoformat()

        params = {
            "limit": batch_size,
            "order": "updated_at asc",
            "status": "any", # Fetch orders of any status
            "fields": "id,order_number,financial_status,fulfillment_status,updated_at,total_price,customer,source_name,line_items" # Fetch needed fields for logging
        }

        if last_sync_time:
            _logger.info(f"Syncing orders updated since: {last_sync_time}")
            params["updated_at_min"] = last_sync_time
        else:
             _logger.info("No previous sync time found. Syncing recent orders (based on batch size and Shopify default).")


        total_processed = 0
        total_created = 0
        total_updated = 0
        new_last_sync_time = last_sync_time # Initialize with the previous sync time

        # Fetch only one batch
        orders_batch, _ = self._fetch_shopify_orders_batch(
            access_token, store_url, api_version, params
        )

        if not orders_batch:
            _logger.info("No new orders found to sync in this run.")
            # Optionally update sync time to current time if no orders found and last_sync_time was set
            # This prevents repeatedly querying the same empty time range.
            # However, the product sync example doesn't seem to do this, so we'll stick to that pattern.
            # if last_sync_time:
            #    set_param(last_sync_time_param, current_sync_start_time)
            return

        last_processed_order_update_time = None

        for order_data in orders_batch:
            shopify_id = str(order_data['id'])
            # Extract details for logging
            order_no = order_data.get('order_number')
            customer_data = order_data.get('customer') or {} # Ensure customer_data is a dict, even if customer is null
            customer_name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip() or "N/A"
            channel = order_data.get('source_name', 'N/A')
            total = order_data.get('total_price', 'N/A')
            payment_status = order_data.get('financial_status', 'N/A')
            fulfillment_status = order_data.get('fulfillment_status', 'N/A')
            item_count = len(order_data.get('line_items', []))

            _logger.info(
                f"Processing Shopify Order ID: {shopify_id} - "
                f"Order No: {order_no}, Cust: {customer_name}, Channel: {channel}, "
                f"Total: {total}, Payment: {payment_status}, Fulfillment: {fulfillment_status}, "
                f"Items: {item_count}"
            )

            vals = {
                'shopify_order_id': shopify_id,
                'shopify_order_number': order_data.get('order_number'),
                'shopify_financial_status': order_data.get('financial_status'),
                'shopify_fulfillment_status': order_data.get('fulfillment_status'),
                'last_sync_date': datetime.now(),
                # 'raw_shopify_data': str(order_data) # Uncomment if needed
            }

            existing_record = self.search([('shopify_order_id', '=', shopify_id)], limit=1)

            try:
                if existing_record:
                    existing_record.write(vals)
                    total_updated += 1
                    _logger.info(f"Updated status for Shopify Order ID: {shopify_id}") # Changed to INFO
                else:
                    self.create(vals)
                    total_created += 1
                    _logger.info(f"Created status record for Shopify Order ID: {shopify_id}") # Changed to INFO

                total_processed += 1
                # Track the updated_at of the last successfully processed order
                last_processed_order_update_time = order_data.get('updated_at')

            except Exception as e:
                 _logger.error(f"Failed to write/create status for Shopify Order ID {shopify_id}: {e}")
                 # Rollback the specific failed transaction and clear cache
                 self.env.cr.rollback()
                 self.env.clear()
                 # Stop processing this batch on error to avoid inconsistent last_sync_time
                 _logger.warning("Stopping batch processing due to error.")
                 return # Exit the sync function

        # Update the last sync time using the 'updated_at' of the last processed order
        if last_processed_order_update_time:
            # Add a microsecond to avoid fetching the same last record again if needed,
            # though Shopify's updated_at_min is usually inclusive.
            # Let's stick to the exact time first, like the product sync likely does.
            new_last_sync_time = last_processed_order_update_time
            set_param(last_sync_time_param, new_last_sync_time)
            _logger.info(f"Shopify order status sync batch finished. Processed: {total_processed}, Created: {total_created}, Updated: {total_updated}.")
            _logger.info(f"Set last sync time ({last_sync_time_param}) to: {new_last_sync_time}")
        else:
             _logger.info(f"Shopify order status sync batch finished. No orders were successfully processed in this batch. Last sync time not updated.")


    @api.model
    def _auto_sync_order_statuses(self):
        """Method called by the cron job."""
        _logger.info("Running auto Shopify order status sync via cron...")
        self.sync_order_statuses()
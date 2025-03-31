# -*- coding: utf-8 -*-
from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_order_id = fields.Char(
        string="Shopify Order ID",
        index=True, # Index for faster searching
        copy=False, # Don't copy when duplicating the order
        help="The unique identifier of the order from Shopify."
    )

    shopify_order_number = fields.Char(
        string="Shopify Order Number",
        copy=False,
        help="The human-readable order number from Shopify (e.g., #1001)."
    )

    # Optional: Add constraint if you want to ensure uniqueness at the Odoo level,
    # but be cautious if multiple Odoo instances might sync from the same Shopify store.
    # _sql_constraints = [
    #     ('shopify_order_id_uniq', 'unique (shopify_order_id)', 'Shopify Order ID must be unique!')
    # ]
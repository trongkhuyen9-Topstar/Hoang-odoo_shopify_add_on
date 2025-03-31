# -*- coding: utf-8 -*-
from odoo import models, fields

class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_customer_id = fields.Char(
        string="Shopify Customer ID",
        index=True, # Index for faster searching
        copy=False, # Don't copy when duplicating partner
        help="The unique identifier of the customer from Shopify."
    )

    # Optional: Add constraint if you want to ensure uniqueness at the Odoo level
    # _sql_constraints = [
    #     ('shopify_customer_id_uniq', 'unique (shopify_customer_id)', 'Shopify Customer ID must be unique!')
    # ]
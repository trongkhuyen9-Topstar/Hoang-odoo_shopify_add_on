# -*- coding: utf-8 -*-
from odoo import models, fields

class AccountPayment(models.Model):
    _inherit = 'account.payment'

    shopify_transaction_id = fields.Char(
        string="Shopify Transaction ID",
        copy=False,
        readonly=True,
        index=True,
        help="The unique identifier of the transaction from Shopify."
    )

    # Optional: Add constraint if needed, though checking in code might be sufficient
    # _sql_constraints = [
    #     ('shopify_transaction_id_uniq', 'unique (shopify_transaction_id)', 'Shopify Transaction ID must be unique!')
    # ]
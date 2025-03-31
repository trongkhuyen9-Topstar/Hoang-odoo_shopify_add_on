{
  'name': 'Shopify Odoo Connector', # More general name
  'version': '1.3', # Incremented version for order sync feature
  'summary': 'Sync products, customers, and orders between Shopify and Odoo', # Updated summary
  'author': 'Franco',
  'depends': ['base', 'product', 'stock', 'sale'], # Added 'sale' dependency
  'installable': True, # Should be installable
  'application': True,
  'category': 'Inventory/Connector', # Updated category
  'license': 'LGPL-3',
  'maintainer': 'Franco',
  'description': """
Sync Products, Customers, and Orders with Shopify
=================================================
* Sync products, variants, images, and inventory levels from Shopify to Odoo Products.
* Creates/updates Odoo warehouses and locations based on Shopify locations.
* Syncs Shopify customers and their addresses to Odoo Partners.
* Syncs Shopify orders to Odoo Sales Orders, including line items.
* Provides views to monitor synced products and orders.
* Includes cron jobs for automatic synchronization.
""", # Enhanced description for order sync

  "data": [
    "security/ir.model.access.csv", # Added security file
    "views/odoo_sync_views.xml",
    "views/shopify_sync_views.xml",
    "views/shopify_order_sync_views.xml" # Added new view file
  ]
}

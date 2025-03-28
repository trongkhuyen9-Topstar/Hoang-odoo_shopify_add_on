{
  'name': 'Shopify to Odoo Product Sync',
  'version': '1.2', # Increment version
  'summary': 'Sync products and order statuses between Shopify and Odoo', # Updated summary
  'author': 'Franco',
  'depends': ['base', 'product', 'stock', 'sale'], # Added 'sale' dependency
  'installable': True,
  'application': True,
  'category': 'Inventory/Connector', # Updated category
  'license': 'LGPL-3',
  'maintainer': 'Franco',
  'description': """
Sync Products and Order Statuses with Shopify
=============================================
* Sync products, variants, images, and inventory levels from Shopify to Odoo.
* Creates/updates Odoo warehouses and locations based on Shopify locations.
* Syncs Shopify order financial and fulfillment statuses to Odoo.
* Provides views to monitor synced data.
* Includes cron jobs for automatic synchronization.
""", # Enhanced description

  "data": [
    "security/ir.model.access.csv", # Added security file
    "views/odoo_sync_views.xml",
    "views/shopify_sync_views.xml",
    "views/shopify_order_sync_views.xml" # Added new view file
  ]
}

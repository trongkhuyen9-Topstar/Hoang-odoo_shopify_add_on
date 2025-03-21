{
  'name': 'Shopify to Odoo Product Sync',
  'version': '1.1',
  'summary': 'Sync products between Shopify and Odoo',
  'author': 'Franco',
  'depends': ['base', 'product', 'stock'],
  'installable': True,
  'application': True,
  'category': 'Inventory',
  'license': 'LGPL-3',
  'maintainer': 'Franco',
  'description': 'Automatically sync products, stock levels, and updates between Shopify and Odoo.',

  "data": [
    "views/odoo_shopify_views.xml"
  ]
}

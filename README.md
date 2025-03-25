# Odoo Shopify Addon

<a href="https://buymeacoffee.com/gerardosociasjr" target="_blank">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" style="height: 60px !important; width: 217px !important;">
</a>

## Overview

This Odoo addon integrates Shopify with Odoo, enabling synchronization of products and other related data.

## Features

- Sync products from Shopify to Odoo and vice versa

## Python Version

This addon requires **Python 3.11+**.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/gerardosocias29/odoo_shopify_addon.git
   ```
2. Move the addon to your Odoo addons directory:
   ```bash
   mv odoo_shopify_addon /path/to/odoo/addons/
   ```
3. Restart Odoo and update the module list:
   ```bash
   odoo -u odoo_shopify_addon
   ```

## Configuration

Modify the `shopify_sync.py` and `odoo_sync.py` files to customize synchronization behavior.

### Setting up IR Configuration Parameters

Ensure that the required environment variables are set in Odoo using `ir.config_parameter`:

1. Go to **Settings > Technical > Parameters > System Parameters**.
2. Add the following parameters:
   - `shopify.access_token`: Your Shopify API access token.
   - `shopify.store_url`: Your Shopify store URL.
   - `shopify.last_export_time`: Set to 2000-01-01 00:00:00 on first use (YYYY-MM-DD HH:MM:SS format). 
   - `shopify.last_sync_time`: Set to 2000-01-01T00:00:00.000000
3. Save the changes and restart Odoo if necessary.

## Setting Up Cron Jobs

To automate synchronization, configure scheduled actions in Odoo:

1. Navigate to **Settings > Technical > Automation > Scheduled Actions**.
2. Create a new scheduled action for Shopify to Odoo sync:
   - **Name**: Auto Sync Products from Shopify
   - **Model**: Shopify Sync (`shopify.sync`)
   - **Method**: `_auto_sync_products`
   - **Interval**: Set as needed (e.g., every hour)
3. Create another scheduled action for Odoo to Shopify sync:
   - **Name**: Auto Sync to Shopify
   - **Model**: Odoo Sync (`odoo.sync`)
   - **Method**: `_auto_sync_to_shopify`
   - **Interval**: Set as needed (e.g., every hour)

Alternatively, execute these methods manually in Odoo shell:

```python
env['shopify.sync']._auto_sync_products()
env['odoo.sync']._auto_sync_to_shopify()
```

## Models

The addon includes the following models:

- OdooSync
- ShopifySync
- ProductProduct
- ProductProductShopify

## License

This project is licensed under the MIT License.

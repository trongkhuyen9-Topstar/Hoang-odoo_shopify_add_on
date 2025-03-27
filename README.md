# Odoo Shopify Sync Addon

<a href="https://buymeacoffee.com/gerardosociasjr" target="_blank">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" style="height: 60px !important; width: 217px !important;">
</a>

## Overview

This Odoo addon provides bi-directional integration between Shopify and Odoo, enabling synchronization of product catalog data, including details, variants, images, and inventory levels.

## Features

*   **Bi-directional Product Sync:**
    *   **Shopify to Odoo:** Imports products, variants, images, descriptions, prices, SKUs, and inventory levels from Shopify into Odoo. Creates or updates corresponding Odoo products.
    *   **Odoo to Shopify:** Exports products, variants, images, descriptions, prices, SKUs, and inventory levels from Odoo to Shopify. Creates or updates corresponding Shopify products.
*   **Variant Handling:** Syncs product variants based on attributes and options. Calculates price differences (`price_extra`) for variants during Shopify-to-Odoo sync.
*   **Image Sync:** Syncs the main product image and variant-specific images.
*   **Inventory Sync:** Updates inventory levels between the two platforms.
    *   *Note:* Shopify-to-Odoo inventory sync currently uses a hardcoded Odoo location named 'Default Shopify Location'.
*   **Timestamp-based Sync:** Uses timestamps (`shopify.last_sync_time` for import, `shopify.last_export_time` for export) to sync only modified products since the last run, improving efficiency.
*   **Manual Sync:** Allows manual triggering of sync for individual products from Odoo to Shopify via a button on the product form.
*   **Automated Sync:** Supports configuration via Odoo Scheduled Actions (cron jobs) for regular, automated synchronization.
*   **Configurable Product Status:** Allows setting the default status (e.g., 'active' or 'draft') for products synced from Odoo to Shopify.
*   **Credentials Management:** Uses Odoo System Parameters for securely storing Shopify API credentials.

*Note: Tax synchronization logic (Shopify to Odoo) is currently commented out in `models/shopify_sync.py`.*
*Note: Automatic synchronization upon saving/creating products in Odoo is currently commented out in `models/odoo_sync.py`.*

## Python Version

This addon requires **Python 3.11+**.

## Installation

1.  Clone the repository:
    ```bash
    git clone https://github.com/gerardosocias29/odoo_shopify_addon.git odoo_shopify_sync
    ```
    *(Note: Cloned into `odoo_shopify_sync` directory)*
2.  Move the addon directory (`odoo_shopify_sync`) to your Odoo instance's addons path:
    ```bash
    mv odoo_shopify_sync /path/to/odoo/addons/
    ```
3.  Restart the Odoo server.
4.  Navigate to **Apps** in Odoo, click **Update Apps List**, remove the "Apps" filter, search for "Shopify to Odoo Product Sync", and click **Activate**.

## Configuration

### 1. Shopify API Credentials

It is recommended to create a **Private App** in your Shopify Admin panel to obtain API credentials.

1.  In Shopify Admin, go to **Apps** -> **Develop apps for your store**.
2.  Create a new Private App.
3.  Configure Admin API permissions. Ensure the app has **read and write access** to:
    *   `Products`
    *   `Inventory`
    *   (Optionally other scopes if needed in the future)
4.  Install the app and note the **API key** and **Admin API access token**. The access token will be used as `shopify.access_token`.

### 2. Odoo System Parameters

Configure the necessary parameters in Odoo for the addon to connect to Shopify.

1.  Activate developer mode in Odoo.
2.  Go to **Settings > Technical > Parameters > System Parameters**.
3.  Create or update the following parameters:
    *   `shopify.access_token`: Your Shopify Admin API access token (from the Private App).
    *   `shopify.store_url`: Your Shopify store URL (e.g., `your-store-name.myshopify.com`).
    *   `shopify.last_sync_time`: Timestamp for Shopify -> Odoo sync. Set to `2000-01-01T00:00:00.000000` (ISO 8601 format) for the initial sync. The addon will update this automatically.
    *   `shopify.last_export_time`: Timestamp for Odoo -> Shopify sync. Set to `2000-01-01 00:00:00` (YYYY-MM-DD HH:MM:SS format) for the initial sync. The addon will update this automatically.
    *   `shopify.product_status`: Default status for products synced from Odoo to Shopify. Use `active` to publish immediately or `draft` to create them as drafts in Shopify. Defaults to `draft` if not set.
4.  Save the changes.

## Usage and Synchronization

### Sync Directions

*   **Shopify to Odoo:** Fetches products updated in Shopify since `shopify.last_sync_time` and creates/updates them in Odoo. Uses `shopify_product_id` (custom field added by this addon) to match products.
*   **Odoo to Shopify:** Pushes products updated in Odoo since `shopify.last_export_time` to Shopify. Uses `shopify_product_id` to match products. If the ID is missing, it creates a new product in Shopify and stores the new Shopify ID back in Odoo.

### Automation (Scheduled Actions)

Configure cron jobs in Odoo for automatic synchronization:

1.  Navigate to **Settings > Technical > Automation > Scheduled Actions**.
2.  Create a new scheduled action for **Shopify to Odoo** sync:
    *   **Name**: Auto Sync Products from Shopify
    *   **Model**: `shopify.sync`
    *   **Action To Do**: Execute Python Code
    *   **Python Code**: `env['shopify.sync']._auto_sync_products()`
    *   **Execute Every**: Set your desired interval (e.g., 1 Hours).
    *   Activate the job.
3.  Create another scheduled action for **Odoo to Shopify** sync:
    *   **Name**: Auto Sync Products to Shopify
    *   **Model**: `odoo.sync`
    *   **Action To Do**: Execute Python Code
    *   **Python Code**: `env['odoo.sync']._auto_sync_to_shopify()`
    *   **Execute Every**: Set your desired interval (e.g., 1 Hours).
    *   Activate the job.

### Manual Synchronization

*   **Full Sync (via Code):** You can trigger a full sync manually via Odoo Shell or Server Actions:
    ```python
    # Sync Shopify -> Odoo (respects last_sync_time)
    env['shopify.sync']._auto_sync_products()

    # Sync Odoo -> Shopify (respects last_export_time)
    env['odoo.sync']._auto_sync_to_shopify()
    ```
*   **Individual Product Sync (Odoo -> Shopify):** Navigate to a specific product form view in Odoo (**Inventory > Products > Products**). A "Sync to Shopify" button is available to push that single product to Shopify immediately.

## Models Involved

*   `shopify.sync`: Handles synchronization from Shopify to Odoo.
*   `odoo.sync`: Handles synchronization from Odoo to Shopify.
*   `product.template`: Inherited to add `shopify_product_id`.
*   `product.product`: Inherited to add `shopify_product_id` and the `sync_to_shopify` button/method.

## Important Notes & Limitations

*   **Tax Sync Disabled:** The logic to sync tax information from Shopify to Odoo is currently commented out. Taxes need to be managed manually in Odoo.
*   **Odoo Auto-Sync Disabled:** Automatic sync when creating or writing product records in Odoo is currently commented out. Sync relies on scheduled actions or manual triggers.
*   **Inventory Location:** The Shopify-to-Odoo sync currently updates inventory in a hardcoded Odoo location named 'Default Shopify Location'. Ensure this location exists or modify the code (`models/shopify_sync.py`) if needed.
*   **API Rate Limits:** Be mindful of Shopify API rate limits, especially during initial syncs or with frequent cron job intervals. The code includes basic handling for 429 errors but might need adjustments for very large catalogs.
*   **Pagination:** The `fetch_shopify_products` function currently fetches only one batch of products (default 50) based on `updated_at_min`. It doesn't fully implement pagination to retrieve all products if more than the batch size were updated since the last sync. This might be a limitation for stores with frequent updates to many products.

## License

This project is licensed under the MIT License.

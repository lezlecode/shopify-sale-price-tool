# 🏷️ Shopify Sale Price Tool

A Streamlit app that lets a merchant upload sale CSV files and automatically sets the `custom.sale_price_dollarlabs` metafield on every matching product variant in a Shopify store.

## What it does

1. **Accepts** one or two CSV files — a 30%-off and/or 40%-off sale list.
2. **Searches** the Shopify store for products whose title starts with the value in the *Title starting* column and filters to variants whose title starts with the *Variant starting* column value.
3. **Writes** a JSON metafield (`custom.sale_price_dollarlabs`) with `{"sale_price": <new_price>}` to every matched variant.
4. **Reports** any unmatched rows and lets you download a CSV of all matched variants.

## CSV format

Both files must follow this column layout (header row required):

| Col | Name | Example |
|-----|------|---------|
| 0 | Title starting | `841186` |
| 1 | *(optional label)* | `RETRO CHIC HI CUT PANT` |
| 2 | Variant starting | `830` |
| 3 | *(optional label)* | `Apricot Blush` |
| 4 | US Price | `32` |
| 5 | New Price | `22.4` |

## Running locally

```bash
# 1. Clone
git clone https://github.com/lezlecode/shopify-sale-price-tool.git
cd shopify-sale-price-tool

# 2. Install deps
pip install -r requirements.txt

# 3. Run
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (already done).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Pick this repo, branch `main`, file `app.py`.
4. Deploy — no extra config needed (credentials are entered in the UI at runtime).

## Shopify requirements

- An **Admin API access token** (`shpat_…`) with the `write_products` scope.
- A variant metafield definition already created in the store:
  - **Namespace:** `custom`
  - **Key:** `sale_price_dollarlabs`
  - **Type:** JSON

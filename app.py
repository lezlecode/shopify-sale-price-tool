import streamlit as st
import pandas as pd
import requests
import json
import time

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Shopify Sale Price Tool",
    page_icon="🏷️",
    layout="centered",
)

# ── Constants ──────────────────────────────────────────────────────────────────
EXAMPLE_CSV = """product_title,variant_title,final_price
841186 - Retro Chic Hi Cut,830,22.40
841384 - Vivid Attraction Hi Cut,476,22.40
853339 - Comfort First Contour Bra,566,40.80
"""

BATCH_SIZE = 25

# ── Session state defaults ──────────────────────────────────────────────────────
if "search_done" not in st.session_state:
    st.session_state.search_done = False
if "matched" not in st.session_state:
    st.session_state.matched = []
if "not_found" not in st.session_state:
    st.session_state.not_found = []
if "meta_done" not in st.session_state:
    st.session_state.meta_done = False
if "meta_errors" not in st.session_state:
    st.session_state.meta_errors = []

# ── GraphQL helpers ─────────────────────────────────────────────────────────────

def make_endpoint(store: str) -> str:
    store = store.strip().rstrip("/")
    if not store.endswith(".myshopify.com"):
        store += ".myshopify.com"
    return f"https://{store}/admin/api/2025-01/graphql.json"


def parse_metafield_id(raw: str):
    """'custom.sale_price_dollarlabs' → ('custom', 'sale_price_dollarlabs')"""
    raw = raw.strip()
    if "." not in raw:
        raise ValueError("Metafield must be in the format  namespace.key")
    namespace, key = raw.split(".", 1)
    return namespace.strip(), key.strip()


def gql(endpoint: str, token: str, query: str, variables: dict):
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    for _ in range(5):
        resp = requests.post(endpoint, headers=headers,
                             json={"query": query, "variables": variables},
                             timeout=30)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 4)))
            continue
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(data["errors"])
        return data
    raise RuntimeError("Max retries exceeded")


SEARCH_QUERY = """
query($query: String!, $after: String) {
  products(first: 10, query: $query, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      variants(first: 100) {
        nodes { id title price sku }
      }
    }
  }
}
"""

METAFIELDS_MUTATION = """
mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id key value }
    userErrors  { field message code }
  }
}
"""


def find_variants(endpoint: str, token: str, product_prefix: str, variant_prefix: str) -> list[dict]:
    """
    Return all variants where:
      - product title  STARTS WITH  product_prefix  (case-insensitive)
      - variant title  STARTS WITH  variant_prefix  (case-insensitive)
    """
    results = []
    search_term = product_prefix.strip().split()[0]
    after = None

    while True:
        data = gql(endpoint, token, SEARCH_QUERY, {"query": f"title:{search_term}*", "after": after})
        page = data["data"]["products"]

        for product in page["nodes"]:
            if not product["title"].strip().lower().startswith(product_prefix.strip().lower()):
                continue
            for variant in product["variants"]["nodes"]:
                if variant["title"].strip().lower().startswith(variant_prefix.strip().lower()):
                    results.append({
                        "product_id":    product["id"].split("/")[-1],
                        "product_title": product["title"],
                        "variant_id":    variant["id"].split("/")[-1],
                        "variant_title": variant["title"],
                        "current_price": variant["price"],
                        "sku":           variant["sku"],
                    })

        if page["pageInfo"]["hasNextPage"]:
            after = page["pageInfo"]["endCursor"]
            time.sleep(0.2)
        else:
            break

    return results


def set_metafields_batch(
    endpoint: str,
    token: str,
    items: list[dict],
    namespace: str,
    key: str,
    json_key: str,
) -> list:
    inputs = [
        {
            "ownerId":   f"gid://shopify/ProductVariant/{item['variant_id']}",
            "namespace": namespace,
            "key":       key,
            "type":      "json",
            "value":     json.dumps({json_key: float(item["final_price"])}),
        }
        for item in items
    ]
    all_errors = []
    for i in range(0, len(inputs), BATCH_SIZE):
        batch = inputs[i : i + BATCH_SIZE]
        result = gql(endpoint, token, METAFIELDS_MUTATION, {"metafields": batch})
        all_errors.extend(result["data"]["metafieldsSet"]["userErrors"])
        time.sleep(0.2)
    return all_errors


# ── UI ─────────────────────────────────────────────────────────────────────────
st.title("🏷️ Shopify Sale Price Tool")
st.caption(
    "Upload a CSV of products, variants and their final sale prices — "
    "review the matches, then confirm to write the metafield."
)

# ── 1. Credentials ─────────────────────────────────────────────────────────────
with st.expander("🔑 Shopify credentials", expanded=True):
    store_input = st.text_input("Store domain", placeholder="yourstore.myshopify.com")
    token_input = st.text_input("Admin API access token", type="password", placeholder="shpat_…")

# ── 2. Metafield settings ───────────────────────────────────────────────────────
with st.expander("⚙️ Metafield settings", expanded=True):
    mf_input = st.text_input(
        "Metafield  (namespace.key)",
        value="custom.sale_price_dollarlabs",
        placeholder="custom.sale_price_dollarlabs",
        help="The variant metafield to write, in namespace.key format.",
    )
    json_key_input = st.text_input(
        "JSON key",
        value="sale_price",
        placeholder="sale_price",
        help='The key inside the JSON object. E.g. "sale_price" writes {"sale_price": 22.40}.',
    )
    try:
        ns_preview, k_preview = parse_metafield_id(mf_input)
        jk_preview = json_key_input.strip() or "sale_price"
        st.caption(f"Will write → `{ns_preview}.{k_preview}` = `{{\"{jk_preview}\": <final_price>}}`")
    except ValueError:
        st.caption("⚠️ Enter metafield in  `namespace.key`  format.")

# ── 3. CSV upload ───────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📂 Upload CSV")

st.download_button(
    label="⬇️ Download example CSV",
    data=EXAMPLE_CSV,
    file_name="sale_prices_example.csv",
    mime="text/csv",
)

st.markdown(
    "Three columns required: **`product_title`** *(or a starting prefix)*, "
    "**`variant_title`** *(or a starting prefix)*, **`final_price`**."
)

uploaded_file = st.file_uploader("Upload your CSV", type="csv")

df_input = None
if uploaded_file:
    try:
        df_raw = pd.read_csv(uploaded_file, dtype=str).dropna(how="all")
        df_raw.columns = df_raw.columns.str.strip().str.lower()
        missing = {"product_title", "variant_title", "final_price"} - set(df_raw.columns)
        if missing:
            st.error(f"CSV is missing column(s): {', '.join(missing)}")
        else:
            df_input = df_raw[["product_title", "variant_title", "final_price"]].copy()
            df_input = df_input[df_input["product_title"].str.strip().ne("")]
            st.success(f"{len(df_input)} row(s) loaded.")
            with st.expander("Preview uploaded file"):
                st.dataframe(df_input, use_container_width=True)
    except Exception as e:
        st.error(f"Could not read CSV: {e}")

# ── 4. Search button ─────────────────────────────────────────────────────────
st.markdown("---")

mf_valid = "." in mf_input
can_search = bool(store_input and token_input and mf_valid and json_key_input.strip() and df_input is not None)

if st.button("🔍 Find matching variants", type="primary", disabled=not can_search):
    # Reset any previous run
    st.session_state.search_done = False
    st.session_state.meta_done   = False
    st.session_state.matched     = []
    st.session_state.not_found   = []
    st.session_state.meta_errors = []

    endpoint = make_endpoint(store_input)
    total    = len(df_input)

    progress = st.progress(0, text="Searching…")
    matched, not_found = [], []

    for idx, row in enumerate(df_input.itertuples(), 1):
        progress.progress(
            idx / total,
            text=f"{idx}/{total}  |  \"{row.product_title}\"  /  \"{row.variant_title}\""
        )
        try:
            hits = find_variants(endpoint, token_input, row.product_title, row.variant_title)
        except Exception as e:
            st.warning(f"Row {idx} search error: {e}")
            hits = []

        if hits:
            for h in hits:
                matched.append({**h, "final_price": row.final_price})
        else:
            not_found.append({
                "product_title": row.product_title,
                "variant_title": row.variant_title,
                "final_price":   row.final_price,
            })

    progress.progress(1.0, text="Search complete ✓")
    st.session_state.matched    = matched
    st.session_state.not_found  = not_found
    st.session_state.search_done = True

# ── 5. Review results ─────────────────────────────────────────────────────────
if st.session_state.search_done:
    matched   = st.session_state.matched
    not_found = st.session_state.not_found

    st.markdown("---")
    st.subheader("🔎 Review before updating")

    if not_found:
        with st.expander(f"⚠️ {len(not_found)} row(s) not found in store", expanded=True):
            st.dataframe(pd.DataFrame(not_found), use_container_width=True)

    if not matched:
        st.error("No variants matched — nothing to update.")
    else:
        # Summary metrics
        n_variants = len(matched)
        n_products = len({r["product_id"] for r in matched})
        c1, c2 = st.columns(2)
        c1.metric("Products matched", n_products)
        c2.metric("Variants to update", n_variants)

        # Full review table — show the columns the merchant cares about
        review_df = pd.DataFrame(matched)[
            ["product_title", "variant_title", "sku", "current_price", "final_price"]
        ].rename(columns={
            "product_title": "Product",
            "variant_title": "Variant",
            "sku":           "SKU",
            "current_price": "Current price",
            "final_price":   "New sale price",
        })
        st.dataframe(review_df, use_container_width=True, hide_index=True)

        # ── 6. Confirm & write ─────────────────────────────────────────────────
        st.markdown("---")

        if not st.session_state.meta_done:
            if st.button(
                f"✅ Confirm & set metafield on {n_variants} variant(s)",
                type="primary",
            ):
                try:
                    namespace, mf_key = parse_metafield_id(mf_input)
                    json_key = json_key_input.strip()
                    endpoint = make_endpoint(store_input)
                except ValueError as e:
                    st.error(str(e))
                    st.stop()

                meta_progress = st.progress(0, text="Writing metafields…")
                meta_progress.progress(0.3, text=f"Sending {n_variants} update(s)…")

                try:
                    errors = set_metafields_batch(
                        endpoint, token_input, matched, namespace, mf_key, json_key
                    )
                    meta_progress.progress(1.0, text="Done ✓")
                    st.session_state.meta_errors = errors
                    st.session_state.meta_done   = True
                    st.rerun()
                except Exception as e:
                    meta_progress.progress(1.0)
                    st.error(f"Request failed: {e}")

        if st.session_state.meta_done:
            errors = st.session_state.meta_errors
            if errors:
                st.error(f"Completed with {len(errors)} error(s):")
                st.json(errors)
            else:
                try:
                    namespace, mf_key = parse_metafield_id(mf_input)
                    json_key = json_key_input.strip()
                except ValueError:
                    namespace, mf_key, json_key = "custom", "sale_price_dollarlabs", "sale_price"

                st.success(
                    f"✅ `{namespace}.{mf_key}` → `{{\"{json_key}\": …}}` "
                    f"set on **{len(matched)} variant(s)**."
                )

            st.download_button(
                label="⬇️ Download results CSV",
                data=pd.DataFrame(matched).to_csv(index=False).encode(),
                file_name="updated_variants.csv",
                mime="text/csv",
            )

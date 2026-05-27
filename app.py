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

# ── Example CSVs ───────────────────────────────────────────────────────────────
EXAMPLE_SEARCH_CSV = """product_title,variant_title,final_price
841186 - Retro Chic Hi Cut,830,22.40
841384 - Vivid Attraction Hi Cut,476,22.40
853339 - Comfort First Contour Bra,566,40.80
"""

EXAMPLE_ID_CSV = """variant_id,final_price
46077732290780,22.40
46077732323548,22.40
45470578802908,11.40
"""

BATCH_SIZE = 25

# ── Session state defaults ──────────────────────────────────────────────────────
for key, default in {
    "search_done": False,
    "matched":     [],
    "not_found":   [],
    "meta_done":   False,
    "meta_errors": [],
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_endpoint(store: str) -> str:
    store = store.strip().rstrip("/")
    if not store.endswith(".myshopify.com"):
        store += ".myshopify.com"
    return f"https://{store}/admin/api/2025-01/graphql.json"


def parse_metafield_id(raw: str):
    raw = raw.strip()
    if "." not in raw:
        raise ValueError("Metafield must be in  namespace.key  format")
    ns, k = raw.split(".", 1)
    return ns.strip(), k.strip()


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


# ── GraphQL queries ─────────────────────────────────────────────────────────────

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

# Fetch up to 25 variant nodes by GID in one call
VARIANTS_BY_ID_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      title
      price
      sku
      product { id title }
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


# ── Core logic ─────────────────────────────────────────────────────────────────

def find_variants_by_prefix(
    endpoint: str, token: str, product_prefix: str, variant_prefix: str
) -> list[dict]:
    """Search: both title fields use startswith matching."""
    results = []
    search_term = product_prefix.strip().split()[0]
    after = None
    while True:
        data = gql(endpoint, token, SEARCH_QUERY,
                   {"query": f"title:{search_term}*", "after": after})
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


def fetch_variants_by_id(
    endpoint: str, token: str, id_price_rows: list[dict]
) -> tuple[list[dict], list[dict]]:
    """
    Direct: look up each variant_id in Shopify and return matched + not_found.
    id_price_rows = [{"variant_id": "123", "final_price": "22.40"}, ...]
    """
    # Build a map variant_id → final_price for quick lookup
    price_map = {str(r["variant_id"]).strip(): r["final_price"] for r in id_price_rows}
    gids      = [f"gid://shopify/ProductVariant/{vid}" for vid in price_map]

    fetched   = {}   # variant_id (numeric str) → node data
    not_found = []

    for i in range(0, len(gids), BATCH_SIZE):
        batch = gids[i : i + BATCH_SIZE]
        data  = gql(endpoint, token, VARIANTS_BY_ID_QUERY, {"ids": batch})
        for node in data["data"]["nodes"]:
            if node is None:
                continue   # ID didn't resolve to a variant
            vid = node["id"].split("/")[-1]
            fetched[vid] = node
        time.sleep(0.2)

    matched = []
    for vid, final_price in price_map.items():
        if vid in fetched:
            node = fetched[vid]
            matched.append({
                "product_id":    node["product"]["id"].split("/")[-1],
                "product_title": node["product"]["title"],
                "variant_id":    vid,
                "variant_title": node["title"],
                "current_price": node["price"],
                "sku":           node["sku"],
                "final_price":   final_price,
            })
        else:
            not_found.append({"variant_id": vid, "final_price": final_price})

    return matched, not_found


def set_metafields_batch(
    endpoint: str, token: str,
    items: list[dict], namespace: str, key: str, json_key: str,
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


def detect_format(df: pd.DataFrame) -> str | None:
    """Return 'id' if variant_id column present, 'search' if product/variant title columns present, else None."""
    cols = set(df.columns.str.strip().str.lower())
    if "variant_id" in cols and "final_price" in cols:
        return "id"
    if "product_title" in cols and "variant_title" in cols and "final_price" in cols:
        return "search"
    return None


# ── UI ─────────────────────────────────────────────────────────────────────────
st.title("🏷️ Shopify Sale Price Tool")
st.caption(
    "Upload a CSV of products/variants and their final sale prices. "
    "Review the matches, then confirm to write the metafield."
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
        help="The variant metafield to write, in namespace.key format.",
    )
    json_key_input = st.text_input(
        "JSON key",
        value="sale_price",
        help='The key inside the JSON object — e.g. "sale_price" writes {"sale_price": 22.40}.',
    )
    try:
        ns_p, k_p = parse_metafield_id(mf_input)
        jk_p = json_key_input.strip() or "sale_price"
        st.caption(f"Will write → `{ns_p}.{k_p}` = `{{\"{jk_p}\": <final_price>}}`")
    except ValueError:
        st.caption("⚠️ Enter metafield in  `namespace.key`  format.")

# ── 3. CSV upload ───────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📂 Upload CSV")

# Two example downloads side by side
dl1, dl2 = st.columns(2)
with dl1:
    st.download_button(
        label="⬇️ Example: search by title",
        data=EXAMPLE_SEARCH_CSV,
        file_name="example_search.csv",
        mime="text/csv",
        use_container_width=True,
        help="product_title, variant_title, final_price",
    )
with dl2:
    st.download_button(
        label="⬇️ Example: direct variant ID",
        data=EXAMPLE_ID_CSV,
        file_name="example_variant_id.csv",
        mime="text/csv",
        use_container_width=True,
        help="variant_id, final_price",
    )

st.markdown(
    "**Format A — search by title:** `product_title`, `variant_title`, `final_price`  \n"
    "**Format B — direct variant ID:** `variant_id`, `final_price`  \n"
    "*The format is detected automatically from the column names.*"
)

uploaded_file = st.file_uploader("Upload your CSV", type="csv")

df_input   = None
csv_format = None   # "search" | "id"

if uploaded_file:
    try:
        df_raw = pd.read_csv(uploaded_file, dtype=str).dropna(how="all")
        df_raw.columns = df_raw.columns.str.strip().str.lower()
        csv_format = detect_format(df_raw)

        if csv_format is None:
            st.error(
                "Could not detect format. Make sure the CSV has either:\n"
                "- `product_title`, `variant_title`, `final_price`  (search format)\n"
                "- `variant_id`, `final_price`  (direct ID format)"
            )
        elif csv_format == "search":
            df_input = df_raw[["product_title", "variant_title", "final_price"]].copy()
            df_input = df_input[df_input["product_title"].str.strip().ne("")]
            st.success(f"✓ Detected **search-by-title** format — {len(df_input)} row(s).")
            with st.expander("Preview"):
                st.dataframe(df_input, use_container_width=True)
        else:  # id
            df_input = df_raw[["variant_id", "final_price"]].copy()
            df_input = df_input[df_input["variant_id"].str.strip().ne("")]
            st.success(f"✓ Detected **direct variant ID** format — {len(df_input)} row(s).")
            with st.expander("Preview"):
                st.dataframe(df_input, use_container_width=True)

    except Exception as e:
        st.error(f"Could not read CSV: {e}")

# ── 4. Search / lookup button ──────────────────────────────────────────────────
st.markdown("---")

mf_valid   = "." in mf_input
can_search = bool(store_input and token_input and mf_valid and json_key_input.strip() and df_input is not None)
btn_label  = "🔍 Find matching variants" if csv_format == "search" else "🔍 Look up variants"

if st.button(btn_label, type="primary", disabled=not can_search):
    st.session_state.search_done = False
    st.session_state.meta_done   = False
    st.session_state.matched     = []
    st.session_state.not_found   = []
    st.session_state.meta_errors = []

    endpoint = make_endpoint(store_input)

    if csv_format == "search":
        # ── Search format: prefix-match product + variant titles ───────────────
        total    = len(df_input)
        progress = st.progress(0, text="Searching…")
        matched, not_found = [], []

        for idx, row in enumerate(df_input.itertuples(), 1):
            progress.progress(
                idx / total,
                text=f"{idx}/{total}  |  \"{row.product_title}\"  /  \"{row.variant_title}\""
            )
            try:
                hits = find_variants_by_prefix(
                    endpoint, token_input, row.product_title, row.variant_title
                )
            except Exception as e:
                st.warning(f"Row {idx} error: {e}")
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

    else:
        # ── ID format: fetch variant details directly ──────────────────────────
        id_price_rows = df_input.to_dict("records")
        with st.spinner(f"Looking up {len(id_price_rows)} variant(s)…"):
            try:
                matched, not_found = fetch_variants_by_id(endpoint, token_input, id_price_rows)
            except Exception as e:
                st.error(f"Lookup failed: {e}")
                st.stop()

    st.session_state.matched     = matched
    st.session_state.not_found   = not_found
    st.session_state.search_done = True

# ── 5. Review ──────────────────────────────────────────────────────────────────
if st.session_state.search_done:
    matched   = st.session_state.matched
    not_found = st.session_state.not_found

    st.markdown("---")
    st.subheader("🔎 Review before updating")

    if not_found:
        label = "variant ID(s)" if csv_format == "id" else "row(s)"
        with st.expander(f"⚠️ {len(not_found)} {label} not found in store", expanded=True):
            st.dataframe(pd.DataFrame(not_found), use_container_width=True)

    if not matched:
        st.error("No variants matched — nothing to update.")
    else:
        n_variants = len(matched)
        n_products = len({r["product_id"] for r in matched})
        c1, c2 = st.columns(2)
        c1.metric("Products matched", n_products)
        c2.metric("Variants to update", n_variants)

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

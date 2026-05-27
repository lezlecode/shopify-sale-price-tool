import streamlit as st
import pandas as pd
import requests
import json
import time
import io

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Shopify Sale Price Tool",
    page_icon="🏷️",
    layout="centered",
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_endpoint(store: str) -> str:
    store = store.strip().rstrip("/")
    if not store.endswith(".myshopify.com"):
        store = store + ".myshopify.com"
    return f"https://{store}/admin/api/2025-01/graphql.json"


def gql(endpoint: str, token: str, query: str, variables: dict, retries: int = 5):
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    for attempt in range(retries):
        resp = requests.post(endpoint, headers=headers,
                             json={"query": query, "variables": variables},
                             timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 4))
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(data["errors"])
        return data
    raise RuntimeError("Max retries exceeded")


SEARCH_QUERY = """
query searchProducts($query: String!, $after: String) {
  products(first: 10, query: $query, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      handle
      variants(first: 100) {
        nodes { id title price sku }
      }
    }
  }
}
"""

METAFIELDS_SET = """
mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id key value }
    userErrors  { field message code }
  }
}
"""

BATCH_SIZE = 25


def find_variants(endpoint: str, token: str, title_prefix: str, variant_prefix: str) -> list[dict]:
    """Return all variants of products whose title starts with title_prefix
       and whose own title starts with variant_prefix."""
    results = []
    after = None
    while True:
        data = gql(endpoint, token, SEARCH_QUERY,
                   {"query": f"title:{title_prefix}*", "after": after})
        page = data["data"]["products"]
        for product in page["nodes"]:
            if not product["title"].upper().startswith(title_prefix.upper()):
                continue
            for variant in product["variants"]["nodes"]:
                if variant["title"].upper().startswith(variant_prefix.upper()):
                    results.append({
                        "product_id":    product["id"].split("/")[-1],
                        "product_title": product["title"],
                        "product_handle": product["handle"],
                        "variant_id":    variant["id"].split("/")[-1],
                        "variant_title": variant["title"],
                        "variant_price": variant["price"],
                        "variant_sku":   variant["sku"],
                    })
        if page["pageInfo"]["hasNextPage"]:
            after = page["pageInfo"]["endCursor"]
            time.sleep(0.2)
        else:
            break
    return results


def set_metafields(endpoint: str, token: str, variant_ids: list[str], sale_price: float):
    """Set custom.sale_price_dollarlabs on each variant."""
    inputs = [
        {
            "ownerId":   f"gid://shopify/ProductVariant/{vid}",
            "namespace": "custom",
            "key":       "sale_price_dollarlabs",
            "type":      "json",
            "value":     json.dumps({"sale_price": sale_price}),
        }
        for vid in variant_ids
    ]
    errors = []
    for i in range(0, len(inputs), BATCH_SIZE):
        batch = inputs[i:i + BATCH_SIZE]
        result = gql(endpoint, token, METAFIELDS_SET, {"metafields": batch})
        errs = result["data"]["metafieldsSet"]["userErrors"]
        if errs:
            errors.extend(errs)
        time.sleep(0.2)
    return errors


def parse_csv(uploaded_file, discount_label: str) -> pd.DataFrame:
    """Read an uploaded CSV and return a normalised DataFrame."""
    df = pd.read_csv(uploaded_file, header=0, dtype=str).dropna(how="all")
    # Columns: title_starting (col0), [style (col1)], variant_starting (col2), [desc (col3)], us_price (col4), new_price (col5)
    df.columns = [str(c) for c in df.columns]
    col_names = list(df.columns)

    out = pd.DataFrame({
        "title_starting":   df.iloc[:, 0].str.strip(),
        "variant_starting": df.iloc[:, 2].str.strip(),
        "us_price":         df.iloc[:, 4].str.strip(),
        "new_price":        df.iloc[:, 5].str.strip(),
        "discount":         discount_label,
    })
    return out[out["title_starting"].notna() & (out["title_starting"] != "")]


# ── UI ─────────────────────────────────────────────────────────────────────────

st.title("🏷️ Shopify Sale Price Tool")
st.markdown(
    "Upload your 30% and/or 40% sale CSV files. "
    "The app will find every matching variant in your store and write the "
    "`custom.sale_price_dollarlabs` metafield automatically."
)

# ── Credentials ────────────────────────────────────────────────────────────────
with st.expander("🔑 Shopify credentials", expanded=True):
    store_input = st.text_input(
        "Store domain",
        placeholder="yourstore.myshopify.com",
        help="Just the domain — no https://",
    )
    token_input = st.text_input(
        "Admin API access token",
        type="password",
        placeholder="shpat_…",
    )

# ── File uploads ───────────────────────────────────────────────────────────────
st.markdown("---")
col1, col2 = st.columns(2)
with col1:
    file_30 = st.file_uploader("Upload **30% off** CSV", type="csv", key="f30")
with col2:
    file_40 = st.file_uploader("Upload **40% off** CSV", type="csv", key="f40")

# ── Preview ────────────────────────────────────────────────────────────────────
rows_30 = rows_40 = None

if file_30:
    rows_30 = parse_csv(file_30, "30%")
    with st.expander(f"Preview 30% file ({len(rows_30)} rows)"):
        st.dataframe(rows_30, use_container_width=True)

if file_40:
    rows_40 = parse_csv(file_40, "40%")
    with st.expander(f"Preview 40% file ({len(rows_40)} rows)"):
        st.dataframe(rows_40, use_container_width=True)

# ── Run ────────────────────────────────────────────────────────────────────────
st.markdown("---")
run_btn = st.button(
    "🚀 Run — search & update metafields",
    type="primary",
    disabled=not (store_input and token_input and (file_30 or file_40)),
)

if run_btn:
    all_rows = pd.concat([df for df in [rows_30, rows_40] if df is not None], ignore_index=True)
    endpoint = make_endpoint(store_input)

    st.markdown(f"**{len(all_rows)} search criteria** across {len([x for x in [file_30, file_40] if x])} file(s).")

    # ── Phase 1: Search ──────────────────────────────────────────────────────
    st.markdown("### Phase 1 — Finding variants")
    search_progress = st.progress(0, text="Starting search…")
    search_status   = st.empty()

    matched_rows = []
    not_found    = []
    errors_search = []

    for i, row in all_rows.iterrows():
        idx   = list(all_rows.index).index(i) + 1
        total = len(all_rows)
        search_progress.progress(idx / total, text=f"Searching {idx}/{total}: {row['title_starting']} / {row['variant_starting']}")

        try:
            variants = find_variants(endpoint, token_input,
                                     row["title_starting"], row["variant_starting"])
        except Exception as e:
            errors_search.append({"row": i, "error": str(e)})
            variants = []

        if not variants:
            not_found.append(row.to_dict())
        else:
            for v in variants:
                matched_rows.append({
                    "discount":         row["discount"],
                    "title_starting":   row["title_starting"],
                    "variant_starting": row["variant_starting"],
                    "us_price":         row["us_price"],
                    "new_price":        row["new_price"],
                    **v,
                })

    search_progress.progress(1.0, text="Search complete ✓")

    matched_df = pd.DataFrame(matched_rows)
    search_status.success(
        f"Found **{len(matched_df)} variant(s)** across **{matched_df['product_title'].nunique() if len(matched_df) else 0} product(s)**."
        + (f"  ⚠️ {len(not_found)} criteria had no match." if not_found else "")
    )

    with st.expander(f"Matched variants ({len(matched_df)})"):
        st.dataframe(matched_df, use_container_width=True)

    if not_found:
        with st.expander(f"⚠️ Not found ({len(not_found)})", expanded=True):
            st.dataframe(pd.DataFrame(not_found), use_container_width=True)

    # ── Phase 2: Metafields ──────────────────────────────────────────────────
    if len(matched_df) > 0:
        st.markdown("### Phase 2 — Writing metafields")
        meta_progress = st.progress(0, text="Starting metafield updates…")
        meta_status   = st.empty()

        # Group by new_price so we can batch per price group
        all_meta_errors = []
        groups = list(matched_df.groupby("new_price"))

        for g_idx, (price_val, group_df) in enumerate(groups):
            meta_progress.progress(
                (g_idx + 1) / len(groups),
                text=f"Writing metafields for sale price {price_val} ({g_idx+1}/{len(groups)})…"
            )
            variant_ids = group_df["variant_id"].tolist()
            try:
                errs = set_metafields(endpoint, token_input, variant_ids, float(price_val))
                all_meta_errors.extend(errs)
            except Exception as e:
                all_meta_errors.append({"field": "request", "message": str(e)})

        meta_progress.progress(1.0, text="Metafields written ✓")

        if all_meta_errors:
            meta_status.error(f"Completed with {len(all_meta_errors)} error(s).")
            st.json(all_meta_errors)
        else:
            meta_status.success(
                f"✅ Successfully set `custom.sale_price_dollarlabs` on **{len(matched_df)} variant(s)**."
            )

        # ── Download results ─────────────────────────────────────────────────
        st.markdown("### Download results")
        csv_bytes = matched_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download matched variants CSV",
            data=csv_bytes,
            file_name="matched_variants.csv",
            mime="text/csv",
        )

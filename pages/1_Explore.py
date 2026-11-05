import math

import streamlit as st

from services import dataset_service as svc
from services.models import AdapterError
from ui import csv_download, render_detail, results_table, setup_page

setup_page("Explore", "🔎")

SIZE_STEPS = [0, 1, 10, 100, 1_000, 10_000, math.inf]  # MB


def _size_label(mb: float) -> str:
    if mb == math.inf:
        return "Any"
    return f"{mb / 1000:g} GB" if mb >= 1000 else f"{mb:g} MB"


# ---- Sidebar filters --------------------------------------------------------
opts = svc.filter_options()
with st.sidebar:
    st.header("Filters")
    licenses = st.multiselect("License", opts["licenses"])
    formats = st.multiselect("File format", opts["formats"], help="Datasets with any of these formats.")
    tags = st.multiselect("Tags", opts["tags"], help="Datasets that have all of these tags.")
    size_lo, size_hi = st.select_slider("Size", options=SIZE_STEPS, value=(0, math.inf),
                                        format_func=_size_label)
    sort = st.selectbox("Sort by", list(svc.SORT_OPTIONS))

# ---- Search bar -------------------------------------------------------------
st.title("Explore datasets")
q_col, live_col = st.columns([5, 1], vertical_alignment="bottom")
query = q_col.text_input("Search", placeholder="e.g. student performance, diabetes, arabic nlp",
                         label_visibility="collapsed")
if live_col.button("Search Kaggle live", width="stretch", disabled=not query.strip(),
                   help="Query Kaggle directly for this keyword and add the results to the local cache."):
    try:
        with st.spinner(f"Searching Kaggle for “{query}”…"):
            n = svc.live_search(query.strip(), pages=2)
        st.toast(f"Fetched {n} datasets from Kaggle")
    except AdapterError as exc:
        st.error(str(exc))

df = svc.search(
    text=query,
    licenses=licenses,
    formats=formats,
    tags=tags,
    min_mb=size_lo or None,
    max_mb=None if size_hi == math.inf else size_hi,
    sort=sort,
)

# ---- Results ----------------------------------------------------------------
info_col, export_col = st.columns([5, 1], vertical_alignment="center")
total = df.attrs.get("total", len(df))
shown = f" (showing the top {len(df):,} — refine to narrow down)" if total > len(df) else ""
info_col.markdown(f"**{total:,}** datasets match{shown}" + (" — select a row to see details" if len(df) else ""))
with export_col:
    csv_download(df, "kaggle_search_results.csv")

if df.empty:
    if svc.stats()["datasets"] == 0:
        st.info("No datasets cached yet. Ask an admin to run a refresh, or type a keyword and click "
                "**Search Kaggle live**.")
    else:
        st.info("No matches. Try fewer filters, or click **Search Kaggle live** to pull more results.")
    st.stop()

selected = results_table(df, "explore")
if selected:
    render_detail(selected)

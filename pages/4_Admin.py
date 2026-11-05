import hmac

import streamlit as st

import config
from services import dataset_service as svc
from services.models import AdapterError
from ui import setup_page

setup_page("Admin", "⚙️")

st.title("Admin")

# ---- Optional password gate -------------------------------------------------
if config.ADMIN_PASSWORD and not st.session_state.get("admin_ok"):
    pw = st.text_input("Admin password", type="password")
    if pw:
        if hmac.compare_digest(pw, str(config.ADMIN_PASSWORD)):
            st.session_state["admin_ok"] = True
            st.rerun()
        st.error("Wrong password")
    st.stop()

# ---- Connection check -------------------------------------------------------
st.subheader("Kaggle connection")
if st.button("Test connection", icon=":material/wifi:"):
    try:
        user = svc.get_adapter("kaggle").check_auth()
        st.success(f"Connected to Kaggle as **{user}**")
    except AdapterError as exc:
        st.error(str(exc))

# ---- Refresh ----------------------------------------------------------------
st.subheader("Refresh metadata")
st.caption("Pulls dataset metadata (not the data files) for each keyword. Each file-format pass "
           "also teaches the app which formats a dataset contains, which powers the File format filter.")

with st.form("refresh"):
    include_popular = st.checkbox("Include popular datasets (no keyword)", value=True)
    keywords_text = st.text_area("Keywords (one per line)",
                                 value="\n".join(config.DEFAULT_REFRESH_KEYWORDS), height=140)
    c1, c2, c3 = st.columns(3)
    pages = c1.number_input("Pages per query (20 datasets each)", 1, 10, config.DEFAULT_PAGES_PER_QUERY)
    sort_by = c2.selectbox("Kaggle sort order", ["hottest", "votes", "updated", "active", "published"])
    file_types = c3.multiselect("Format passes", config.KAGGLE_FILE_TYPES, default=config.KAGGLE_FILE_TYPES)
    submitted = st.form_submit_button("Refresh", type="primary", icon=":material/sync:")

keywords = ([""] if include_popular else []) + [k.strip() for k in keywords_text.splitlines() if k.strip()]
keywords = list(dict.fromkeys(keywords))  # dedupe, keep order; "" = no keyword
if not keywords:
    keywords = [""]
max_calls = len(keywords) * (1 + len(file_types)) * int(pages)
st.caption(f"Up to **{max_calls}** Kaggle API calls (≈{max_calls * 20:,} dataset rows, many overlapping).")

if submitted:
    bar = st.progress(0.0, text="Starting…")
    try:
        result = svc.refresh(keywords, int(pages), file_types, sort_by=sort_by,
                             progress=lambda frac, label: bar.progress(frac, text=label))
    except AdapterError as exc:
        bar.empty()
        st.error(str(exc))
    else:
        bar.progress(1.0, text="Done")
        st.success(f"Fetched {result.fetched:,} rows · {result.new:,} new datasets added.")
        if result.errors:
            with st.expander(f"{len(result.errors)} queries had errors"):
                st.code("\n".join(result.errors))

# ---- History ----------------------------------------------------------------
st.subheader("Refresh history")
hist = svc.refresh_history()
if hist.empty:
    st.caption("No refreshes yet.")
else:
    st.dataframe(hist, hide_index=True, width="stretch")

s = svc.stats()
st.caption(f"Database: `{config.DB_PATH}` · {s['datasets']:,} datasets · {s['bookmarks']:,} bookmarks")

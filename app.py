"""Home page. Run with:  streamlit run app.py"""
import streamlit as st

import config
from services import dataset_service as svc
from ui import setup_page

setup_page("Home", "🏠")

st.title(config.APP_TITLE)
st.caption(f"Version {config.APP_VERSION} · internal team tool for finding research datasets on Kaggle")

s = svc.stats()
c1, c2, c3 = st.columns(3)
c1.metric("Datasets cached", f"{s['datasets']:,}")
c2.metric("Team bookmarks", f"{s['bookmarks']:,}")
c3.metric("Last refresh", (s["last_refresh"] or "never")[:16].replace("T", " "),
          help=f"Status: {s['last_status']}" if s["last_status"] else None)

if s["datasets"] == 0:
    st.info("The local cache is empty. Go to **Admin** and click **Refresh** to pull dataset metadata "
            "from Kaggle, or use **Search Kaggle live** on the Explore page.", icon=":material/info:")

st.subheader("Get started")
col1, col2, col3 = st.columns(3)
with col1, st.container(border=True):
    st.markdown("**🔎 Explore**\n\nSearch by keyword, filter by license, format, size and tags, "
                "sort by popularity or update date, and export results to CSV.")
    st.page_link("pages/1_Explore.py", label="Open Explore", icon=":material/arrow_forward:")
with col2, st.container(border=True):
    st.markdown("**★ Bookmarks**\n\nSee what you and your teammates saved, with notes on why each "
                "dataset is useful.")
    st.page_link("pages/2_Bookmarks.py", label="Open Bookmarks", icon=":material/arrow_forward:")
with col3, st.container(border=True):
    st.markdown("**⚙️ Admin**\n\nCheck the Kaggle connection and refresh the cached metadata.")
    st.page_link("pages/3_Admin.py", label="Open Admin", icon=":material/arrow_forward:")

st.caption("Always check each dataset's license on Kaggle before using it in teaching or research.")

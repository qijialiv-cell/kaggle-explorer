import streamlit as st

from services import dataset_service as svc
from ui import csv_download, current_user, render_detail, results_table, setup_page

setup_page("Bookmarks", "⭐")

st.title("Bookmarks")

EVERYONE = "Everyone"
users = svc.bookmark_users()
me = current_user()
choices = [EVERYONE] + users
default = choices.index(me) if me in users else 0

c1, c2 = st.columns([2, 4], vertical_alignment="bottom")
who = c1.selectbox("Show bookmarks from", choices, index=default)

df = svc.list_bookmarks(None if who == EVERYONE else who)

if who == EVERYONE and not df.empty:
    # Same dataset can be bookmarked by several people; show it once, with everyone's names.
    names = df.groupby("id")["added_by"].apply(lambda s: ", ".join(sorted(set(s))))
    df = df.drop_duplicates("id").copy()
    df["added_by"] = df["id"].map(names)

with c2:
    csv_download(df, "kaggle_bookmarks.csv")

if df.empty:
    st.info("No bookmarks yet. Open a dataset on the **Explore** page and use the Bookmarks tab.")
    st.page_link("pages/1_Explore.py", label="Go to Explore", icon=":material/arrow_forward:")
    st.stop()

st.markdown(f"**{len(df):,}** bookmarked datasets — select a row to see details")
selected = results_table(df, f"bookmarks_{who}")
if selected:
    render_detail(selected)

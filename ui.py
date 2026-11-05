"""Shared Streamlit pieces used by every page."""
from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

import auth
import config
from services import dataset_service as svc
from services.models import AdapterError

EXPORT_COLUMNS = ["title", "ref", "owner", "license", "size_mb", "vote_count", "download_count",
                  "usability", "last_updated", "formats", "tags", "url"]


@st.cache_resource
def _init_db_once() -> bool:
    svc.ensure_db()
    return True


def setup_page(title: str, icon: str = "📊") -> None:
    st.set_page_config(page_title=f"{title} · {config.APP_TITLE}", page_icon=icon, layout="wide")
    _init_db_once()
    email = auth.require_login()  # stops the page here if not signed in
    with st.sidebar:
        st.caption(f"Signed in as **{email}**")
        if st.button("Sign out", icon=":material/logout:"):
            auth.logout()
            st.rerun()
        st.divider()


def current_user() -> str:
    return auth.current_email() or "team"


def fmt_size(num_bytes) -> str:
    if num_bytes is None or pd.isna(num_bytes):
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1000
    return "—"


def _badge(text: str, color: str = "blue") -> str:
    clean = text.replace("[", "(").replace("]", ")")
    return f":{color}-badge[{clean}]"


def results_table(df: pd.DataFrame, state_key: str) -> str | None:
    """Render a selectable results table; returns the selected dataset id (or None)."""
    df = df.reset_index(drop=True)
    # A new key whenever the result set changes, so a stale row selection doesn't carry over.
    digest = hashlib.md5("|".join(df["id"].head(500)).encode()).hexdigest()[:10]
    event = st.dataframe(
        df,
        key=f"{state_key}_{digest}",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        width="stretch",
        height=min(38 + 35 * len(df), 520),
        column_order=["title", "owner", "vote_count", "download_count", "size_mb", "usability",
                      "last_updated", "license", "formats", "tags", "bookmarks", "url"],
        column_config={
            "title": st.column_config.TextColumn("Title", width="large"),
            "owner": "Owner",
            "vote_count": st.column_config.NumberColumn("Votes", format="%d"),
            "download_count": st.column_config.NumberColumn("Downloads", format="%d"),
            "size_mb": st.column_config.NumberColumn("Size (MB)", format="%.1f"),
            "usability": st.column_config.ProgressColumn("Usability", min_value=0, max_value=1,
                                                         format="%.2f"),
            "last_updated": st.column_config.DatetimeColumn("Updated", format="YYYY-MM-DD"),
            "license": "License",
            "formats": "Formats",
            "tags": "Tags",
            "bookmarks": st.column_config.NumberColumn("★", help="Team bookmarks", format="%d"),
            "url": st.column_config.LinkColumn("Kaggle", display_text="Open ↗"),
        },
    )
    rows = event.selection.rows if event and event.selection else []
    return df.loc[rows[0], "id"] if rows else None


def csv_download(df: pd.DataFrame, filename: str, label: str = "Export CSV") -> None:
    cols = [c for c in (["added_by", "note", "bookmarked_at"] + EXPORT_COLUMNS) if c in df.columns]
    data = df[cols].to_csv(index=False).encode("utf-8-sig")  # BOM so Excel opens UTF-8 correctly
    st.download_button(label, data=data, file_name=filename, mime="text/csv",
                       disabled=df.empty, icon=":material/download:")


def render_detail(dataset_id: str) -> None:
    ds = svc.get_dataset(dataset_id)
    if ds is None:
        st.warning("Dataset not found in the local cache.")
        return

    if not ds.get("details_fetched_at"):
        with st.spinner("Loading files and description from Kaggle…"):
            try:
                svc.load_details(dataset_id)
                ds = svc.get_dataset(dataset_id)
            except AdapterError as exc:
                st.warning(f"Couldn't load full details: {exc}")

    with st.container(border=True):
        head, link = st.columns([4, 1], vertical_alignment="center")
        head.subheader(ds["title"])
        if ds.get("subtitle"):
            head.caption(ds["subtitle"])
        link.link_button("Kaggle", ds["url"], icon=":material/open_in_new:", width="stretch")

        m = st.columns(4)
        m[0].metric("Votes", f"{ds['vote_count'] or 0:,}")
        m[1].metric("Downloads", f"{ds['download_count'] or 0:,}")
        m[2].metric("Size", fmt_size(ds["size_bytes"]))
        m[3].metric("Usability", f"{ds['usability']:.2f}" if ds["usability"] is not None else "—")

        st.markdown(
            f"**Owner:** {ds['owner'] or '—'}  ·  **License:** {ds['license'] or '—'}  ·  "
            f"**Updated:** {(ds['last_updated'] or '—')[:10]}  ·  **Ref:** `{ds['ref']}`")
        if ds["formats"]:
            st.markdown("**Formats:** " + " ".join(_badge(f, "green") for f in ds["formats"]))
        if ds["tags"]:
            st.markdown("**Tags:** " + " ".join(_badge(t) for t in ds["tags"]))

        tab_desc, tab_files, tab_bm = st.tabs(
            ["Description", f"Files ({len(ds['files'])})", f"Bookmarks ({len(ds['bookmarked_by'])})"])

        with tab_desc:
            if ds.get("description"):
                with st.container(height=400):
                    st.markdown(ds["description"])
            else:
                st.caption("No description available.")

        with tab_files:
            if ds["files"]:
                files = pd.DataFrame(ds["files"])
                files["size"] = files["size_bytes"].map(fmt_size)
                st.dataframe(files[["name", "size"]], hide_index=True, width="stretch",
                             height=min(38 + 35 * len(files), 400))
            else:
                st.caption("No file list cached yet.")
            if st.button("Refresh details from Kaggle", key=f"refresh_{dataset_id}",
                         icon=":material/refresh:"):
                try:
                    with st.spinner("Refreshing…"):
                        svc.load_details(dataset_id, force=True)
                    st.rerun()
                except AdapterError as exc:
                    st.error(str(exc))

        with tab_bm:
            _bookmark_controls(ds)


def _bookmark_controls(ds: dict) -> None:
    user = current_user()
    mine = next((b for b in ds["bookmarked_by"] if b["added_by"] == user), None)

    for b in ds["bookmarked_by"]:
        note = f" — {b['note']}" if b["note"] else ""
        st.markdown(f"★ **{b['added_by']}** ({b['created_at'][:10]}){note}")

    with st.form(key=f"bm_{ds['id']}", border=False):
        note = st.text_input("Note (optional)", value=(mine or {}).get("note") or "",
                             placeholder="Why is this useful? e.g. 'good for week-5 lab'")
        c1, c2 = st.columns(2)
        save = c1.form_submit_button("Update bookmark" if mine else f"Bookmark as {user}",
                                     icon=":material/star:", type="primary")
        remove = c2.form_submit_button("Remove my bookmark", disabled=mine is None)
    if save:
        svc.add_bookmark(ds["id"], user, note)
        st.toast("Bookmark saved")
        st.rerun()
    if remove:
        svc.remove_bookmark(ds["id"], user)
        st.toast("Bookmark removed")
        st.rerun()

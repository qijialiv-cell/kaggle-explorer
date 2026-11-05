"""Everything the UI needs: sync from adapters into SQLite, search/filter, details, bookmarks."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

import config
from database.db import get_conn, init_db
from services.kaggle_api import KaggleAdapter, formats_from_filenames
from services.models import DatasetAdapter, DatasetRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------
_adapter_cache: dict[str, DatasetAdapter] = {}


def get_adapter(source: str = "kaggle") -> DatasetAdapter:
    if source not in _adapter_cache:
        if source == "kaggle":
            _adapter_cache[source] = KaggleAdapter(
                username=config.get_setting("kaggle_username"),
                key=config.get_setting("kaggle_key"),
                api_token=config.get_setting("kaggle_api_token"),
            )
        else:  # future: "huggingface", "uci"
            raise ValueError(f"No adapter for source '{source}' yet")
    return _adapter_cache[source]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
_UPSERT_SQL = """
INSERT INTO datasets (id, source, ref, title, subtitle, description, owner, creator, url,
                      license, size_bytes, download_count, vote_count, view_count,
                      usability, last_updated, fetched_at)
VALUES (:id, :source, :ref, :title, :subtitle, :description, :owner, :creator, :url,
        :license, :size_bytes, :download_count, :vote_count, :view_count,
        :usability, :last_updated, :fetched_at)
ON CONFLICT(id) DO UPDATE SET
    title = excluded.title,
    subtitle = excluded.subtitle,
    description = COALESCE(excluded.description, datasets.description),
    owner = excluded.owner,
    creator = excluded.creator,
    url = excluded.url,
    license = excluded.license,
    size_bytes = excluded.size_bytes,
    download_count = excluded.download_count,
    vote_count = excluded.vote_count,
    view_count = excluded.view_count,
    usability = excluded.usability,
    last_updated = excluded.last_updated,
    fetched_at = excluded.fetched_at
"""


def upsert_records(records: list[DatasetRecord]) -> int:
    """Save records; returns how many were new to the database."""
    if not records:
        return 0
    now = _now()
    new_count = 0
    with get_conn() as conn:
        for r in records:
            exists = conn.execute("SELECT 1 FROM datasets WHERE id = ?", (r.id,)).fetchone()
            new_count += 0 if exists else 1
            row = {k: getattr(r, k) for k in (
                "source", "ref", "title", "subtitle", "description", "owner", "creator", "url",
                "license", "size_bytes", "download_count", "vote_count", "view_count",
                "usability", "last_updated")}
            row.update(id=r.id, fetched_at=now)
            conn.execute(_UPSERT_SQL, row)
            if r.tags:
                conn.execute("DELETE FROM dataset_tags WHERE dataset_id = ?", (r.id,))
                conn.executemany("INSERT OR IGNORE INTO dataset_tags VALUES (?, ?)",
                                 [(r.id, t) for t in r.tags])
            conn.executemany("INSERT OR IGNORE INTO dataset_formats VALUES (?, ?)",
                             [(r.id, f) for f in r.formats])
    return new_count


@dataclass
class RefreshResult:
    fetched: int = 0
    new: int = 0
    errors: list[str] = field(default_factory=list)


def refresh(keywords: list[str], pages: int, file_types: list[str], sort_by: str = "hottest",
            source: str = "kaggle",
            progress: Callable[[float, str], None] | None = None) -> RefreshResult:
    """Pull metadata for each keyword x file type x page and store it.

    The extra per-file-type passes are how we learn each dataset's formats
    (Kaggle's list endpoint doesn't return them directly).
    """
    adapter = get_adapter(source)
    passes = ["all", *file_types]
    keywords = keywords or [""]
    total = len(keywords) * len(passes) * pages
    step = 0
    result = RefreshResult()

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO refresh_log (source, started_at, params, status) VALUES (?, ?, ?, 'running')",
            (source, _now(), json.dumps({"keywords": keywords, "pages": pages,
                                          "file_types": file_types, "sort_by": sort_by})))
        log_id = cur.lastrowid

    for kw in keywords:
        for ft in passes:
            for page in range(1, pages + 1):
                step += 1
                label = f"'{kw or '(all)'}' · {ft} · page {page}"
                if progress:
                    progress(step / total, label)
                try:
                    records = adapter.search(query=kw, page=page, sort_by=sort_by, file_type=ft)
                except Exception as exc:
                    result.errors.append(f"{label}: {exc}")
                    break
                result.fetched += len(records)
                result.new += upsert_records(records)
                if len(records) < 20:  # last page
                    break
                time.sleep(config.REQUEST_PAUSE_SECONDS)

    status = "ok" if not result.errors else ("partial" if result.fetched else "failed")
    with get_conn() as conn:
        conn.execute(
            "UPDATE refresh_log SET finished_at=?, fetched=?, new_count=?, status=?, message=? WHERE id=?",
            (_now(), result.fetched, result.new, status, "\n".join(result.errors[:20]) or None, log_id))
    return result


def live_search(query: str, pages: int = 1, sort_by: str = "hottest",
                source: str = "kaggle") -> int:
    """Search the source directly and cache the results. Returns how many were fetched."""
    adapter = get_adapter(source)
    fetched = 0
    for page in range(1, pages + 1):
        records = adapter.search(query=query, page=page, sort_by=sort_by)
        upsert_records(records)
        fetched += len(records)
        if len(records) < 20:
            break
    return fetched


def load_details(dataset_id: str, force: bool = False) -> None:
    """Fetch files + description for one dataset (once; cached afterwards)."""
    with get_conn() as conn:
        row = conn.execute("SELECT source, ref, description, details_fetched_at FROM datasets WHERE id=?",
                           (dataset_id,)).fetchone()
    if row is None or (row["details_fetched_at"] and not force):
        return
    adapter = get_adapter(row["source"])
    files = adapter.get_files(row["ref"])
    description = row["description"] or adapter.get_description(row["ref"])
    save_details(dataset_id, files, description)


MAX_FILES_STORED = 300  # keeps the cache small for datasets with thousands of files


def save_details(dataset_id: str, files: list, description: str | None, conn=None) -> None:
    """Store a dataset's file list + description and derive formats from file extensions."""
    def _write(c):
        c.execute("DELETE FROM dataset_files WHERE dataset_id=?", (dataset_id,))
        c.executemany("INSERT OR IGNORE INTO dataset_files VALUES (?, ?, ?)",
                      [(dataset_id, f.name, f.size_bytes) for f in files[:MAX_FILES_STORED]])
        c.executemany("INSERT OR IGNORE INTO dataset_formats VALUES (?, ?)",
                      [(dataset_id, f) for f in formats_from_filenames([f.name for f in files])])
        c.execute("UPDATE datasets SET description=COALESCE(?, description), details_fetched_at=? WHERE id=?",
                  (description, _now(), dataset_id))
    if conn is not None:
        _write(conn)
    else:
        with get_conn() as c:
            _write(c)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
SORT_OPTIONS = {
    "Most votes": "d.vote_count DESC NULLS LAST",
    "Most downloads": "d.download_count DESC NULLS LAST",
    "Recently updated": "d.last_updated DESC NULLS LAST",
    "Highest usability": "d.usability DESC NULLS LAST",
    "Largest": "d.size_bytes DESC NULLS LAST",
    "Smallest": "d.size_bytes ASC NULLS LAST",
    "Title A–Z": "d.title COLLATE NOCASE ASC",
}

_LIST_COLUMNS = """
    d.id, d.source, d.ref, d.title, d.subtitle, d.owner, d.license, d.size_bytes,
    d.vote_count, d.download_count, d.view_count, d.usability, d.last_updated, d.url,
    (SELECT group_concat(tag, ', ') FROM dataset_tags t WHERE t.dataset_id = d.id) AS tags,
    (SELECT group_concat(format, ', ') FROM dataset_formats f WHERE f.dataset_id = d.id) AS formats,
    (SELECT count(*) FROM bookmarks b WHERE b.dataset_id = d.id) AS bookmarks
"""


def _finish_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df["size_mb"] = (df["size_bytes"].fillna(0) / 1_000_000).round(2)
    df["last_updated"] = pd.to_datetime(df["last_updated"], errors="coerce", utc=True)
    return df


def search(text: str = "", licenses: list[str] | None = None, formats: list[str] | None = None,
           tags: list[str] | None = None, min_mb: float | None = None, max_mb: float | None = None,
           sort: str = "Most votes", limit: int = 1000) -> pd.DataFrame:
    """Return up to `limit` matches; the total match count is in df.attrs['total']."""
    where, params = ["1=1"], []

    for word in text.split():
        like = f"%{word}%"
        where.append("""(d.title LIKE ? OR d.subtitle LIKE ? OR d.description LIKE ? OR d.ref LIKE ?
                          OR EXISTS (SELECT 1 FROM dataset_tags t WHERE t.dataset_id=d.id AND t.tag LIKE ?))""")
        params += [like] * 5
    if licenses:
        where.append(f"d.license IN ({','.join('?' * len(licenses))})")
        params += licenses
    if formats:  # any of the chosen formats
        where.append(f"d.id IN (SELECT dataset_id FROM dataset_formats WHERE format IN "
                     f"({','.join('?' * len(formats))}))")
        params += formats
    for tag in tags or []:  # all of the chosen tags
        where.append("EXISTS (SELECT 1 FROM dataset_tags t WHERE t.dataset_id=d.id AND t.tag = ?)")
        params.append(tag)
    if min_mb is not None:
        where.append("COALESCE(d.size_bytes, 0) >= ?")
        params.append(min_mb * 1_000_000)
    if max_mb is not None:
        where.append("COALESCE(d.size_bytes, 0) <= ?")
        params.append(max_mb * 1_000_000)

    order = SORT_OPTIONS.get(sort, SORT_OPTIONS["Most votes"])
    where_sql = " AND ".join(where)
    # Pick the page of ids first (cheap), then compute tags/formats only for those rows.
    sql = (f"SELECT {_LIST_COLUMNS} FROM datasets d WHERE d.id IN "
           f"(SELECT d.id FROM datasets d WHERE {where_sql} ORDER BY {order} LIMIT ?) ORDER BY {order}")
    with get_conn() as conn:
        total = conn.execute(f"SELECT count(*) FROM datasets d WHERE {where_sql}", params).fetchone()[0]
        df = pd.read_sql_query(sql, conn, params=[*params, limit])
    df = _finish_frame(df)
    df.attrs["total"] = total
    return df


def get_dataset(dataset_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["tags"] = [r[0] for r in conn.execute(
            "SELECT tag FROM dataset_tags WHERE dataset_id=? ORDER BY tag", (dataset_id,))]
        data["formats"] = [r[0] for r in conn.execute(
            "SELECT format FROM dataset_formats WHERE dataset_id=? ORDER BY format", (dataset_id,))]
        data["files"] = [dict(r) for r in conn.execute(
            "SELECT name, size_bytes FROM dataset_files WHERE dataset_id=? ORDER BY name", (dataset_id,))]
        data["bookmarked_by"] = [dict(r) for r in conn.execute(
            "SELECT added_by, note, created_at FROM bookmarks WHERE dataset_id=? ORDER BY created_at",
            (dataset_id,))]
    return data


def filter_options() -> dict[str, list[str]]:
    with get_conn() as conn:
        licenses = [r[0] for r in conn.execute(
            "SELECT license FROM datasets WHERE license IS NOT NULL GROUP BY license ORDER BY count(*) DESC")]
        formats = [r[0] for r in conn.execute(
            "SELECT format FROM dataset_formats GROUP BY format ORDER BY count(*) DESC")]
        tags = [r[0] for r in conn.execute(
            "SELECT tag FROM dataset_tags GROUP BY tag ORDER BY count(*) DESC, tag LIMIT 300")]
    return {"licenses": licenses, "formats": formats, "tags": tags}


def stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT count(*) FROM datasets").fetchone()[0]
        n_bookmarks = conn.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
        last = conn.execute(
            "SELECT finished_at, status FROM refresh_log WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return {"datasets": total, "bookmarks": n_bookmarks,
            "last_refresh": last["finished_at"] if last else None,
            "last_status": last["status"] if last else None}


def refresh_history(limit: int = 20) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(
            "SELECT started_at, finished_at, status, fetched, new_count, params, message "
            "FROM refresh_log ORDER BY id DESC LIMIT ?", conn, params=[limit])


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------
def add_bookmark(dataset_id: str, added_by: str, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bookmarks (dataset_id, added_by, note, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(dataset_id, added_by) DO UPDATE SET note = excluded.note",
            (dataset_id, added_by, note or None, _now()))


def remove_bookmark(dataset_id: str, added_by: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM bookmarks WHERE dataset_id=? AND added_by=?", (dataset_id, added_by))


def list_bookmarks(added_by: str | None = None) -> pd.DataFrame:
    sql = f"""SELECT b.added_by, b.note, b.created_at AS bookmarked_at, {_LIST_COLUMNS}
              FROM bookmarks b JOIN datasets d ON d.id = b.dataset_id"""
    params: list = []
    if added_by:
        sql += " WHERE b.added_by = ?"
        params.append(added_by)
    sql += " ORDER BY b.created_at DESC"
    with get_conn() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    return _finish_frame(df)


def bookmark_users() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT added_by FROM bookmarks ORDER BY added_by")]


def ensure_db() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Downloading single files for the Analyze page
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = config.DB_PATH.parent / "downloads"
MAX_ANALYZE_MB = float(config.get_setting("max_analyze_mb", 150))  # keeps memory use safe on the cloud
KEEP_DOWNLOADS = 6  # most recent files kept on disk; older ones are deleted


def _download_key(dataset_id: str, file_name: str) -> str:
    """Short folder name per file (long nested Kaggle paths overflow Windows' 260-char limit)."""
    return hashlib.md5(f"{dataset_id}|{file_name}".encode()).hexdigest()[:16]


def download_for_analysis(dataset_id: str, file_name: str) -> Path:
    """Download one file of a dataset (reusing a previous download) and return its path."""
    with get_conn() as conn:
        row = conn.execute("SELECT source, ref FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown dataset {dataset_id}")
    dest = DOWNLOAD_DIR / _download_key(dataset_id, file_name)
    done = [p for p in dest.rglob("*") if p.is_file()] if dest.exists() else []
    if done:
        dest.touch()
        return done[0]

    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    path = get_adapter(row["source"]).download_file(row["ref"], file_name, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    _prune_downloads()
    return dest / path.relative_to(tmp)


def _prune_downloads() -> None:
    dirs = [d for d in DOWNLOAD_DIR.iterdir() if d.is_dir() and not d.name.endswith(".part")]
    for old in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)[KEEP_DOWNLOADS:]:
        shutil.rmtree(old, ignore_errors=True)

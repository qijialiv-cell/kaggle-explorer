"""Thin SQLite helpers. A new connection per call keeps things safe across Streamlit threads."""
from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path

from config import DB_PATH

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_datasets.db"
SEED_GZ_PATH = SEED_PATH.with_name("seed_datasets.db.gz")


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seed cache: the committed metadata snapshot a fresh (or outdated) server starts from
# ---------------------------------------------------------------------------
def _seed_file() -> Path | None:
    return SEED_GZ_PATH if SEED_GZ_PATH.exists() else SEED_PATH if SEED_PATH.exists() else None


def _seed_id(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _db_seed_id(path: Path) -> str | None:
    try:
        with closing(sqlite3.connect(path)) as c:
            row = c.execute("SELECT value FROM app_meta WHERE key='seed_id'").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


# Team data that lives only in the running database and must survive a seed upgrade.
_CARRY_OVER_SQL = """
-- datasets someone bookmarked (or opened details for) that the new seed doesn't have
INSERT OR IGNORE INTO datasets SELECT * FROM old.datasets
 WHERE id IN (SELECT dataset_id FROM old.bookmarks) AND id NOT IN (SELECT id FROM datasets);
INSERT OR IGNORE INTO dataset_tags SELECT * FROM old.dataset_tags
 WHERE dataset_id IN (SELECT dataset_id FROM old.bookmarks);
-- files/descriptions loaded on demand that the new seed lacks
UPDATE datasets SET
    description = COALESCE(description, (SELECT o.description FROM old.datasets o WHERE o.id = datasets.id)),
    details_fetched_at = (SELECT o.details_fetched_at FROM old.datasets o WHERE o.id = datasets.id)
 WHERE details_fetched_at IS NULL
   AND id IN (SELECT id FROM old.datasets WHERE details_fetched_at IS NOT NULL);
INSERT OR IGNORE INTO dataset_files SELECT * FROM old.dataset_files
 WHERE dataset_id IN (SELECT id FROM datasets);
INSERT OR IGNORE INTO dataset_formats SELECT * FROM old.dataset_formats
 WHERE dataset_id IN (SELECT id FROM datasets);
INSERT OR IGNORE INTO bookmarks SELECT * FROM old.bookmarks
 WHERE dataset_id IN (SELECT id FROM datasets);
"""


def _restore_seed() -> None:
    """Start from the committed seed; when a newer seed ships, upgrade and keep team data."""
    seed = _seed_file()
    if seed is None:
        return
    seed_id = _seed_id(seed)
    if DB_PATH.exists() and _db_seed_id(DB_PATH) == seed_id:
        return

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".restoring")
    tmp.unlink(missing_ok=True)
    if seed.suffix == ".gz":
        with gzip.open(seed, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(seed, tmp)

    with closing(sqlite3.connect(tmp)) as new:
        new.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if DB_PATH.exists():
            with closing(sqlite3.connect(DB_PATH)) as old:  # fold any WAL into the old file first
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                old.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # old DBs may lack newer tables
            new.execute("ATTACH DATABASE ? AS old", (str(DB_PATH),))
            new.executescript(_CARRY_OVER_SQL)
            new.commit()
            new.execute("DETACH DATABASE old")
        new.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('seed_id', ?)", (seed_id,))
        new.commit()

    for suffix in ("-wal", "-shm"):  # a stale journal must never be applied to the new file
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    tmp.replace(DB_PATH)  # atomic, so a half-written file is never used


def init_db(restore_seed: bool = True) -> None:
    if restore_seed:
        _restore_seed()
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

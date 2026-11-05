"""Thin SQLite helpers. A new connection per call keeps things safe across Streamlit threads."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_datasets.db"


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


def init_db() -> None:
    # First start on a fresh server (e.g. Streamlit Cloud): begin from the committed seed cache.
    if not DB_PATH.exists() and SEED_PATH.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SEED_PATH, DB_PATH)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

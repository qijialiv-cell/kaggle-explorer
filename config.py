"""App settings. Values come from environment variables first, then .streamlit/secrets.toml."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

APP_TITLE = "Kaggle Research Dataset Explorer"
APP_VERSION = "1.0"


def get_setting(name: str, default=None):
    """Look up `name` in env (upper-case) then Streamlit secrets (lower-case)."""
    env_val = os.environ.get(name.upper())
    if env_val:
        return env_val
    try:
        import streamlit as st

        return st.secrets.get(name.lower(), default)
    except Exception:  # no secrets file, or not running under Streamlit
        return default


DB_PATH = Path(get_setting("db_path", BASE_DIR / "data" / "datasets.db"))

# Default sync settings shown on the Admin page.
DEFAULT_REFRESH_KEYWORDS = ["education", "health", "finance", "climate", "nlp"]
DEFAULT_PAGES_PER_QUERY = 2          # Kaggle returns 20 datasets per page
KAGGLE_FILE_TYPES = ["csv", "json", "sqlite", "parquet", "bigQuery"]
REQUEST_PAUSE_SECONDS = 0.3          # be polite to the Kaggle API

# Optional: if set, the Admin page asks for it before allowing a refresh.
ADMIN_PASSWORD = get_setting("admin_password")

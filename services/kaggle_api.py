"""Kaggle adapter: wraps the official `kaggle` package and maps results to DatasetRecord."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from services.models import AdapterError, DatasetAdapter, DatasetFile, DatasetRecord

try:
    # Use the OS certificate store, so HTTPS works behind school/corporate TLS inspection.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

KAGGLE_BASE_URL = "https://www.kaggle.com/datasets/"

# Kaggle's `file_type` filter values -> display names stored in dataset_formats.
FILE_TYPE_LABELS = {
    "csv": "CSV",
    "json": "JSON",
    "sqlite": "SQLite",
    "parquet": "Parquet",
    "bigQuery": "BigQuery",
}

# File extensions -> display names (used when we list a dataset's files).
EXTENSION_LABELS = {
    ".csv": "CSV", ".tsv": "CSV",
    ".json": "JSON", ".jsonl": "JSON",
    ".sqlite": "SQLite", ".db": "SQLite", ".sqlite3": "SQLite",
    ".parquet": "Parquet",
    ".xlsx": "Excel", ".xls": "Excel",
    ".txt": "Text",
    ".png": "Images", ".jpg": "Images", ".jpeg": "Images",
    ".wav": "Audio", ".mp3": "Audio",
    ".h5": "HDF5", ".hdf5": "HDF5",
    ".pdf": "PDF",
    ".xml": "XML",
}


def formats_from_filenames(names: list[str]) -> list[str]:
    found = {EXTENSION_LABELS.get(Path(n).suffix.lower()) for n in names}
    return sorted(f for f in found if f)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class KaggleAdapter(DatasetAdapter):
    source = "kaggle"

    def __init__(self, username: str | None = None, key: str | None = None,
                 api_token: str | None = None):
        # The kaggle package reads credentials from env vars or ~/.kaggle/.
        if api_token:
            os.environ["KAGGLE_API_TOKEN"] = api_token
        if username and key:
            os.environ["KAGGLE_USERNAME"] = username
            os.environ["KAGGLE_KEY"] = key
        self._api = None

    # -- connection -------------------------------------------------------
    def _client(self):
        if self._api is not None:
            return self._api
        # Importing `kaggle` tries to authenticate and prints help text on failure;
        # silence that and authenticate explicitly so we can raise a clean error.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from kaggle.api.kaggle_api_extended import KaggleApi

            api = KaggleApi()
            try:
                api.authenticate()
            except SystemExit as exc:  # kaggle calls exit(1) when no credentials are found
                raise AdapterError(
                    "Kaggle credentials not found. Add kaggle_username / kaggle_key "
                    "(or kaggle_api_token) to .streamlit/secrets.toml, or put kaggle.json in ~/.kaggle/."
                ) from exc
            except Exception as exc:  # network / TLS / invalid token
                raise AdapterError(f"Could not connect to Kaggle: {exc}") from exc
        self._api = api
        return api

    def check_auth(self) -> str:
        """Return the authenticated username (raises AdapterError if not configured)."""
        api = self._client()
        return api.config_values.get("username") or "authenticated"

    # -- adapter interface -----------------------------------------------
    def search(self, query: str = "", page: int = 1, sort_by: str = "hottest",
               file_type: str | None = None) -> list[DatasetRecord]:
        api = self._client()
        for attempt in range(3):  # retry transient network/TLS drops
            try:
                items = api.dataset_list(
                    search=query or None,
                    page=page,
                    sort_by=sort_by,
                    file_type=file_type if file_type and file_type != "all" else None,
                ) or []
                break
            except Exception as exc:
                if attempt == 2:
                    raise AdapterError(f"Kaggle search failed: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))

        fmt = FILE_TYPE_LABELS.get(file_type or "")
        return [self._to_record(d, fmt) for d in items if d is not None]

    def get_files(self, ref: str) -> list[DatasetFile]:
        api = self._client()
        files: list[DatasetFile] = []
        token = None
        try:
            for _ in range(10):  # up to ~1000 files; enough for browsing
                resp = api.dataset_list_files(ref, page_token=token, page_size=100)
                if resp is None:
                    break
                if getattr(resp, "error_message", None):
                    raise AdapterError(resp.error_message)
                for f in resp.files or []:
                    files.append(DatasetFile(name=f.name, size_bytes=f.total_bytes))
                token = getattr(resp, "next_page_token", None)
                if not token:
                    break
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"Could not list files for {ref}: {exc}") from exc
        return files

    def get_description(self, ref: str) -> str | None:
        api = self._client()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                meta_path = api.dataset_metadata(ref, path=tmp)
                data = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception as exc:
            raise AdapterError(f"Could not load description for {ref}: {exc}") from exc
        info = data.get("info", data) if isinstance(data, dict) else {}
        return info.get("description") or None

    def download_file(self, ref: str, file_name: str, dest_dir: Path) -> Path:
        """Download one file of a dataset into dest_dir (unzipped) and return its local path."""
        api = self._client()
        dest_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):  # retry transient network/TLS drops
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    api.dataset_download_file(ref, file_name, path=str(dest_dir), force=True,
                                              quiet=True, unzip=True)
                break
            except Exception as exc:
                if attempt == 2 or "404" in str(exc) or "403" in str(exc):
                    raise AdapterError(f"Could not download {file_name} from {ref}: {exc}") from exc
                time.sleep(2 * (attempt + 1))
        found = [p for p in dest_dir.rglob("*") if p.is_file()]
        if not found:
            raise AdapterError(f"Kaggle returned no file for {file_name}.")
        return max(found, key=lambda p: p.stat().st_mtime)

    # -- mapping ----------------------------------------------------------
    def _to_record(self, d, fmt: str | None) -> DatasetRecord:
        ref = d.ref
        url = d.url or ""
        if not url.startswith("http"):
            url = KAGGLE_BASE_URL + ref
        tags = [t.name for t in (d.tags or []) if getattr(t, "name", None)]
        return DatasetRecord(
            source=self.source,
            ref=ref,
            title=d.title or ref,
            subtitle=d.subtitle or None,
            description=d.description or None,
            owner=d.owner_name or ref.split("/")[0],
            creator=d.creator_name or None,
            url=url,
            license=d.license_name or None,
            size_bytes=d.total_bytes,
            download_count=d.download_count,
            vote_count=d.vote_count,
            view_count=d.view_count,
            usability=d.usability_rating,
            last_updated=_iso(d.last_updated),
            tags=tags,
            formats=[fmt] if fmt else [],
        )

"""Pre-download Kaggle metadata, file lists and descriptions into the local SQLite cache.

Run from the project folder (needs the Kaggle token in .streamlit/secrets.toml or env):

    python scripts/build_cache.py                 # both phases
    python scripts/build_cache.py --phase list    # only harvest dataset listings
    python scripts/build_cache.py --phase details --workers 6
    python scripts/build_cache.py --export        # write data/seed_datasets.db.gz for deployment

It is resumable: listings upsert, and the details phase skips datasets already done.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

secrets = ROOT / ".streamlit" / "secrets.toml"
if secrets.exists() and not os.environ.get("KAGGLE_API_TOKEN"):
    token = tomllib.loads(secrets.read_text(encoding="utf-8")).get("kaggle_api_token")
    if token:
        os.environ["KAGGLE_API_TOKEN"] = token

import config  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from services import dataset_service as svc  # noqa: E402
from services.kaggle_api import KaggleAdapter  # noqa: E402

SORT_ORDERS = ["votes", "hottest", "updated", "published", "active"]
MAX_PAGES = 500  # Kaggle stops returning results around page 500 (10k rows per query)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Throttle:
    """Global spacing between Kaggle API calls; slows down and pauses on HTTP 429."""

    def __init__(self, interval: float):
        self.base = interval
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            slot = max(now, self._next)
            self._next = slot + self.interval
        time.sleep(max(0.0, slot - now))

    def ok(self) -> None:
        """Each successful call nudges the pace back toward the base rate."""
        with self._lock:
            self.interval = max(self.base, self.interval * 0.995)

    def rate_limited(self) -> None:
        with self._lock:
            self.interval = min(self.interval * 1.3, 6.0)
            self._next = time.time() + 60
            log(f"429 rate limit: pausing 60s, interval now {self.interval:.2f}s")


THROTTLE = Throttle(0.7)


def _is_429(exc: BaseException) -> bool:
    return "429" in str(exc) or "Too Many Requests" in str(exc)


def harvest_listings(sorts: list[str], max_pages: int) -> None:
    adapter = KaggleAdapter()
    for sort in sorts:
        new_total, empty_streak = 0, 0
        for page in range(1, max_pages + 1):
            records = None
            for _attempt in range(6):
                THROTTLE.wait()
                try:
                    records = adapter.search(page=page, sort_by=sort)
                    THROTTLE.ok()
                    break
                except Exception as exc:
                    if _is_429(exc):
                        THROTTLE.rate_limited()
                    else:
                        log(f"[{sort}] page {page}: {str(exc)[:150]}")
                        time.sleep(10)
            if records is None:
                log(f"[{sort}] page {page}: giving up on this page")
                continue
            if not records:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            new_total += svc.upsert_records(records)
            if page % 25 == 0:
                log(f"[{sort}] page {page}: +{new_total} new so far · total {svc.stats()['datasets']:,}")
        log(f"[{sort}] done: {new_total} new · total {svc.stats()['datasets']:,}")


_shared: KaggleAdapter | None = None
_lock = threading.Lock()


def _adapter() -> KaggleAdapter:
    """One authenticated client shared by all workers (authenticating per thread races)."""
    global _shared
    with _lock:
        if _shared is None:
            _shared = KaggleAdapter()
            _shared.check_auth()
    return _shared


def _fetch(ref: str):
    a = _adapter()
    files, last = None, None
    for attempt in range(6):
        THROTTLE.wait()
        try:
            files = a.get_files(ref)
            THROTTLE.ok()
            break
        except Exception as exc:
            last = exc
            if _is_429(exc):
                THROTTLE.rate_limited()
            elif "404" in str(exc):
                files = []  # e.g. BigQuery-backed datasets have no file list
                break
            else:
                time.sleep(3 * (attempt + 1))
    if files is None:
        raise last
    desc = None
    for _attempt in range(4):
        THROTTLE.wait()
        try:
            desc = a.get_description(ref)
            THROTTLE.ok()
            break
        except Exception as exc:
            if _is_429(exc):
                THROTTLE.rate_limited()
            else:
                break  # some datasets (BigQuery, removed) have no metadata page
    return files, desc


def fetch_details(workers: int, limit: int | None) -> None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ref FROM datasets WHERE details_fetched_at IS NULL "
            "ORDER BY vote_count DESC NULLS LAST").fetchall()
    todo = [(r["id"], r["ref"]) for r in rows][: limit or None]
    log(f"details: {len(todo):,} datasets to fetch with {workers} workers")
    done = failed = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, ref): ds_id for ds_id, ref in todo}
        batch: list = []
        for fut in as_completed(futures):
            ds_id = futures[fut]
            try:
                files, desc = fut.result()
                batch.append((ds_id, files, desc))
            except BaseException as exc:  # kaggle can raise SystemExit; never let it kill the run
                if isinstance(exc, KeyboardInterrupt):
                    raise
                failed += 1
                if failed <= 20:
                    log(f"failed {ds_id}: {type(exc).__name__}: {str(exc)[:150]}")
            if len(batch) >= 50:
                _flush(batch)
                done += len(batch)
                batch = []
                rate = done / (time.time() - start)
                left = (len(todo) - done - failed) / rate if rate else 0
                log(f"details: {done:,}/{len(todo):,} done · {failed} failed · "
                    f"~{left/60:.0f} min left · interval {THROTTLE.interval:.2f}s")
        _flush(batch)
        done += len(batch)
    log(f"details finished: {done:,} saved · {failed} failed")


def _flush(batch: list) -> None:
    if not batch:
        return
    with get_conn() as conn:
        for ds_id, files, desc in batch:
            svc.save_details(ds_id, files, desc, conn=conn)


def export_seed() -> None:
    """Write a bookmark-free, compressed copy of the cache for deployment."""
    tmp = ROOT / "data" / "seed_export.db"
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    dst.execute("DELETE FROM bookmarks")
    dst.commit()
    dst.execute("PRAGMA journal_mode=DELETE")
    dst.execute("VACUUM")
    n, d = dst.execute("SELECT count(*), sum(details_fetched_at IS NOT NULL) FROM datasets").fetchone()
    dst.close()
    out = ROOT / "data" / "seed_datasets.db.gz"
    with open(tmp, "rb") as f_in, gzip.open(out, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw_mb, gz_mb = tmp.stat().st_size / 1e6, out.stat().st_size / 1e6
    tmp.unlink()
    log(f"exported {out.name}: {n:,} datasets ({d:,} with details) · {raw_mb:.1f} MB raw → {gz_mb:.1f} MB gz")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--phase", choices=["list", "details", "all"], default="all")
    p.add_argument("--sorts", default=",".join(SORT_ORDERS))
    p.add_argument("--pages", type=int, default=MAX_PAGES)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--limit", type=int, default=None, help="max datasets for the details phase")
    p.add_argument("--export", action="store_true", help="only export the deployment seed")
    a = p.parse_args()

    if sys.platform == "win32" and not a.export:
        # Stop Windows idle-sleeping while this process runs (released automatically on exit).
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED

    init_db(restore_seed=False)  # never swap the working cache for the seed
    if a.export:
        export_seed()
        return
    if a.phase in ("list", "all"):
        harvest_listings(a.sorts.split(","), a.pages)
    if a.phase in ("details", "all"):
        fetch_details(a.workers, a.limit)
    log(f"cache now: {svc.stats()['datasets']:,} datasets")


if __name__ == "__main__":
    main()

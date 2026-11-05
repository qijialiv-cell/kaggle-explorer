"""Descriptive analysis of one tabular file (pure pandas, no Streamlit).

Typical use:
    df, truncated = load_table(path, max_rows=300_000)
    overview(df); column_profile(df); numeric_summary(df)
    frequency_table(df, "gender"); correlations(df)
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

TABULAR_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl", ".parquet"}


def is_tabular(file_name: str) -> bool:
    name = file_name.replace("\\", "/")
    if "__MACOSX/" in name or Path(name).name.startswith("._"):  # macOS archive junk
        return False
    return Path(name).suffix.lower() in TABULAR_EXTENSIONS


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_table(path: str | Path, max_rows: int | None = None) -> tuple[pd.DataFrame, bool]:
    """Read a CSV/TSV/Excel/JSON/Parquet file. Returns (df, truncated)."""
    path = Path(path)
    ext = path.suffix.lower()
    nrows = max_rows + 1 if max_rows else None  # one extra row tells us if the file was cut

    if ext in (".csv", ".tsv"):
        sep = "\t" if ext == ".tsv" else ","
        df = None
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(path, sep=sep, nrows=nrows, encoding=enc, low_memory=False,
                                 on_bad_lines="skip")
                break
            except UnicodeDecodeError:
                continue
        if df is None:
            raise ValueError("Could not decode the file as text.")
        if df.shape[1] == 1 and ext == ".csv":  # maybe ; or | separated
            sniffed = pd.read_csv(path, sep=None, engine="python", nrows=nrows, encoding=enc,
                                  on_bad_lines="skip")
            if sniffed.shape[1] > 1:
                df = sniffed
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, nrows=nrows)  # first sheet
    elif ext in (".json", ".jsonl"):
        df = _read_json(path, lines=ext == ".jsonl")
    elif ext == ".parquet":
        df = pd.read_parquet(path)
    else:
        raise ValueError(f"Unsupported file type: {ext or 'no extension'}")

    df.columns = [str(c) for c in df.columns]
    truncated = bool(max_rows) and len(df) > max_rows
    if truncated:
        df = df.head(max_rows)
    return _parse_dates(df), truncated


def _read_json(path: Path, lines: bool) -> pd.DataFrame:
    """Records/columns JSON, JSON Lines, or one object wrapping a list of records."""
    try:
        return pd.read_json(path, lines=lines)
    except ValueError:
        pass
    try:
        return pd.read_json(path, lines=not lines)
    except ValueError:
        pass
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v]
        data = max(lists, key=len) if lists else [data]  # the biggest list is usually the table
    return pd.json_normalize(data, max_level=1)


def _parse_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Turn text columns that are clearly dates into datetime columns."""
    for col in df.columns:
        s = df[col]
        if s.dtype != object:
            continue
        sample = s.dropna().astype(str).head(200)
        if sample.empty or not sample.str.contains(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{4}-\d{2}").mean() > 0.9:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(s, errors="coerce", format="mixed")
        if parsed.notna().sum() >= 0.9 * s.notna().sum():
            df[col] = parsed
    return df


# ---------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------
def column_kind(s: pd.Series) -> str:
    """numeric · categorical · boolean · datetime · text · id"""
    n = s.notna().sum()
    try:
        unique = s.nunique(dropna=True)
    except TypeError:  # lists/dicts from nested JSON
        return "text"
    if pd.api.types.is_bool_dtype(s) or (unique == 2 and set(s.dropna().unique()) <= {0, 1}):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    if pd.api.types.is_numeric_dtype(s):
        if unique == n and n > 50 and pd.api.types.is_integer_dtype(s):
            return "id"
        if unique <= 10 and n > 50:
            return "categorical"  # e.g. ratings 1–5 or coded groups
        return "numeric"
    if n and unique == n and n > 50:
        return "id"
    if unique <= max(30, 0.05 * n):
        return "categorical"
    return "text"


def column_kinds(df: pd.DataFrame) -> dict[str, str]:
    return {c: column_kind(df[c]) for c in df.columns}


def numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c, k in column_kinds(df).items()
            if k in ("numeric", "categorical") and pd.api.types.is_numeric_dtype(df[c])
            and not pd.api.types.is_bool_dtype(df[c])]


def categorical_columns(df: pd.DataFrame) -> list[str]:
    return [c for c, k in column_kinds(df).items() if k in ("categorical", "boolean")]


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
def overview(df: pd.DataFrame) -> dict:
    kinds = column_kinds(df)
    cells = df.size or 1
    return {
        "rows": len(df),
        "columns": df.shape[1],
        "numeric_columns": sum(k == "numeric" for k in kinds.values()),
        "categorical_columns": sum(k in ("categorical", "boolean") for k in kinds.values()),
        "datetime_columns": sum(k == "datetime" for k in kinds.values()),
        "text_columns": sum(k in ("text", "id") for k in kinds.values()),
        "missing_cells": int(df.isna().sum().sum()),
        "missing_pct": float(round(100 * df.isna().sum().sum() / cells, 2)),
        "duplicate_rows": int(_safe_duplicated(df).sum()),
        "memory_mb": float(round(df.memory_usage(deep=True).sum() / 1e6, 2)),
    }


def _safe_duplicated(df: pd.DataFrame) -> pd.Series:
    try:
        return df.duplicated()
    except TypeError:  # unhashable cells (lists/dicts from JSON)
        return df.astype(str).duplicated()


def column_profile(df: pd.DataFrame) -> pd.DataFrame:
    """One row per column: type, completeness, distinct values and an example."""
    kinds = column_kinds(df)
    rows = []
    for c in df.columns:
        s = df[c]
        non_null = int(s.notna().sum())
        try:
            unique = int(s.nunique(dropna=True))
            top = s.mode(dropna=True)
            top_value = str(top.iloc[0]) if len(top) else ""
        except TypeError:
            unique, top_value = int(s.astype(str).nunique()), ""
        rows.append({
            "column": c,
            "type": kinds[c],
            "dtype": str(s.dtype),
            "non_null": non_null,
            "missing": len(s) - non_null,
            "missing_pct": round(100 * (len(s) - non_null) / max(len(s), 1), 2),
            "unique": unique,
            "most_common": top_value[:60],
        })
    return pd.DataFrame(rows)


def numeric_summary(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """Count, mean, SD, min, quartiles, max, IQR, skewness, kurtosis and IQR outliers."""
    cols = columns or numeric_columns(df)
    rows = []
    for c in cols:
        s = pd.to_numeric(df[c], errors="coerce").dropna()
        if s.empty:
            continue
        q1, med, q3 = s.quantile([0.25, 0.5, 0.75])
        iqr = q3 - q1
        outliers = int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum())
        rows.append({
            "column": c, "count": int(s.count()), "missing": int(df[c].isna().sum()),
            "mean": s.mean(), "std": s.std(), "min": s.min(), "q1": q1, "median": med,
            "q3": q3, "max": s.max(), "iqr": iqr, "skew": s.skew(), "kurtosis": s.kurt(),
            "zeros": int((s == 0).sum()), "outliers_iqr": outliers,
        })
    return pd.DataFrame(rows)


def frequency_table(df: pd.DataFrame, column: str, top: int = 20) -> pd.DataFrame:
    """Counts and percentages of the most common values (missing values included)."""
    s = df[column]
    try:
        counts = s.value_counts(dropna=False)
    except TypeError:
        counts = s.astype(str).value_counts(dropna=False)
    total = max(len(s), 1)
    out = counts.head(top).rename_axis("value").reset_index(name="count")
    if len(counts) > top:
        rest = int(counts.iloc[top:].sum())
        out = pd.concat([out, pd.DataFrame([{"value": f"(other {len(counts) - top} values)",
                                             "count": rest}])], ignore_index=True)
    out["value"] = out["value"].map(lambda v: "(missing)" if pd.isna(v) else str(v))
    out["percent"] = (100 * out["count"] / total).round(2)
    out["cumulative_pct"] = out["percent"].cumsum().round(2)
    return out


def group_summary(df: pd.DataFrame, value: str, by: str, top: int = 20) -> pd.DataFrame:
    """Mean/median/SD of a numeric column for each group of a categorical column."""
    s = pd.to_numeric(df[value], errors="coerce")
    g = s.groupby(df[by].astype(str)).agg(["count", "mean", "median", "std", "min", "max"])
    return g.sort_values("count", ascending=False).head(top).reset_index()


def correlations(df: pd.DataFrame, method: str = "pearson", max_columns: int = 30) -> pd.DataFrame:
    cols = [c for c in numeric_columns(df) if df[c].nunique() > 1][:max_columns]
    if len(cols) < 2:
        return pd.DataFrame()
    return df[cols].apply(pd.to_numeric, errors="coerce").corr(method=method)


def top_correlations(corr: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Strongest column pairs, by absolute correlation."""
    if corr.empty:
        return pd.DataFrame(columns=["column_a", "column_b", "r"])
    mask = np.triu(np.ones(corr.shape, dtype=bool), k=1)
    pairs = corr.where(mask).stack().dropna().rename("r").reset_index()
    pairs.columns = ["column_a", "column_b", "r"]
    return pairs.reindex(pairs["r"].abs().sort_values(ascending=False).index).head(n).reset_index(drop=True)

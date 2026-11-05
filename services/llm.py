"""Google Gemini research assistant (no Streamlit here; the page handles the UI).

    ctx = build_context(dataset, file_name, df)
    for chunk in stream_reply(api_key, model, ctx, history, "Suggest hypotheses"):
        print(chunk, end="")

The API key is passed in per call and never stored by this module.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

import pandas as pd

from services import analysis as an

try:  # trust the OS certificate store (school TLS inspection)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# "-latest" aliases always point at Google's current models; numbered models get retired.
PREFERRED_MODELS = ["gemini-flash-latest", "gemini-pro-latest", "gemini-flash-lite-latest"]
_NOT_CHAT = ("embedding", "image", "banana", "tts", "audio", "live", "transcribe", "robotics",
             "computer-use", "customtools", "veo", "imagen")
MAX_CONTEXT_CHARS = 40_000
MAX_HISTORY_TURNS = 20  # older messages are dropped to keep requests small

SYSTEM_PROMPT = """You are a research assistant for faculty at the Higher Colleges of Technology (UAE).
They browse Kaggle datasets and want help turning a dataset into research and teaching work.

Ground every answer in the DATA CONTEXT below (dataset metadata and descriptive statistics computed
from the actual file). Use exact column names in `backticks`. When you suggest an analysis, name the
statistical method, the variables involved, and the assumptions to check. Be explicit about what the
data cannot show (e.g. causation from observational data, sampling bias, missing values, licence
limits). If a question needs information that is not in the context, say so instead of guessing.
Keep answers well structured with short headings and bullet points. The user may also ask about
other topics; answer those helpfully too."""

# Quick actions shown as buttons: label -> prompt
QUICK_PROMPTS: dict[str, str] = {
    "Research questions": (
        "Propose 6 research questions this dataset could answer. For each: the question, the columns "
        "involved, the analysis method, and why it matters. Mix descriptive, comparative and predictive "
        "questions."),
    "Hypotheses": (
        "Write 5 testable hypotheses (H0 and H1) based on these columns. For each, give the statistical "
        "test to use, the assumptions to check, and what result would support it."),
    "Analysis plan": (
        "Suggest a step-by-step analysis plan: data cleaning (refer to the missing values, outliers and "
        "types you see), exploratory analysis, statistical tests or models, and visualisations."),
    "Data quality": (
        "Review the data quality: missing values, suspicious ranges, outliers, duplicates, imbalanced "
        "categories, ID or leakage columns, and anything that limits the validity of conclusions. "
        "Suggest a fix for each issue."),
    "Explain the stats": (
        "Explain the key descriptive statistics of this file in plain language for a non-specialist: "
        "what stands out in the distributions, frequencies and correlations, and what it means."),
    "Teaching ideas": (
        "Suggest 4 classroom activities or student projects using this dataset for higher-education "
        "students, with learning outcomes, the skills practised, and difficulty level."),
    "Paper outline": (
        "Draft an outline for a short research paper using this dataset: title ideas, research gap, "
        "method, expected results section structure, limitations and ethical considerations."),
}


class GeminiError(Exception):
    """A user-facing problem talking to Gemini (bad key, quota, network)."""


def _client(api_key: str):
    from google import genai

    from google.genai import types

    if not api_key:
        raise GeminiError("Paste a Gemini API key first.")
    # Retry a busy model only briefly; stream_reply then moves on to another model, which is
    # much faster than the SDK's default long back-off.
    retry = types.HttpRetryOptions(attempts=2, initial_delay=1.0, max_delay=4.0)
    return genai.Client(api_key=api_key.strip(),
                        http_options=types.HttpOptions(retry_options=retry, timeout=120_000))


def _friendly(exc: Exception) -> GeminiError:
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        msg = exc.message or str(exc)
        if exc.code in (400, 401, 403) and "key" in msg.lower():
            return GeminiError("Gemini rejected the API key. Check it at https://aistudio.google.com/apikey.")
        if exc.code == 429:
            return GeminiError("Gemini quota or rate limit reached. Wait a minute or try a lighter model.")
        if exc.code == 404:
            return GeminiError("That Gemini model isn't available for this key. Pick another model.")
        return GeminiError(f"Gemini error {exc.code}: {msg[:300]}")
    return GeminiError(f"Could not reach Gemini: {str(exc)[:300]}")


def list_models(api_key: str) -> list[str]:
    """Gemini models this key can use for text generation (also validates the key)."""
    try:
        names = []
        client = _client(api_key)  # keep a reference: the pager needs the client open while iterating
        for m in client.models.list():
            actions = m.supported_actions or []
            name = (m.name or "").removeprefix("models/")
            if "generateContent" in actions and name.startswith("gemini") and not any(
                    x in name for x in _NOT_CHAT):
                names.append(name)
    except GeminiError:
        raise
    except Exception as exc:
        raise _friendly(exc) from exc
    preferred = [m for m in PREFERRED_MODELS if m in names]
    return preferred + sorted((n for n in names if n not in preferred), key=_newest_first)


def _newest_first(name: str):
    """Higher version first; stable releases before previews."""
    m = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(m.group(1)) if m else 0.0
    return (-version, "preview" in name, name)


# ---------------------------------------------------------------------------
# Context: what Gemini is told about the chosen data
# ---------------------------------------------------------------------------
def build_context(dataset: dict | None, file_name: str | None = None, df: pd.DataFrame | None = None,
                  sample_rows: int = 0, truncated: bool = False) -> str:
    """A compact text summary of the dataset and file. Raw rows are only included if asked."""
    parts: list[str] = []
    if dataset:
        parts.append("## Dataset (Kaggle)")
        parts.append(f"Title: {dataset.get('title')}\nRef: {dataset.get('ref')}\nURL: {dataset.get('url')}")
        meta = {k: dataset.get(k) for k in ("subtitle", "owner", "license", "last_updated",
                                            "vote_count", "download_count", "usability")}
        parts.append("\n".join(f"{k}: {v}" for k, v in meta.items() if v not in (None, "")))
        if dataset.get("tags"):
            parts.append("Tags: " + ", ".join(dataset["tags"]))
        if dataset.get("description"):
            parts.append("Description:\n" + dataset["description"][:6000])
        if dataset.get("files"):
            names = [f["name"] for f in dataset["files"][:40]]
            parts.append(f"Files ({len(dataset['files'])}): " + ", ".join(names))

    if df is not None and file_name:
        ov = an.overview(df)
        parts.append(f"\n## File analysed: {file_name}")
        if truncated:
            parts.append(f"(Statistics use the first {len(df):,} rows of a larger file.)")
        parts.append(", ".join(f"{k}={v}" for k, v in ov.items()))
        parts.append("\n### Columns\n" + an.column_profile(df).to_csv(index=False))
        num = an.numeric_summary(df)
        if not num.empty:
            parts.append("### Numeric summary\n" + num.round(4).to_csv(index=False))
        for col in an.categorical_columns(df)[:8]:
            parts.append(f"### Frequencies: {col}\n" + an.frequency_table(df, col, top=10)
                         .to_csv(index=False))
        corr = an.correlations(df, method="spearman")
        if not corr.empty:
            parts.append("### Strongest Spearman correlations\n"
                         + an.top_correlations(corr, n=12).round(3).to_csv(index=False))
        if sample_rows:
            parts.append(f"### First {sample_rows} rows\n" + df.head(sample_rows).to_csv(index=False))

    text = "\n".join(parts)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n…(context trimmed)"
    return text


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
_RETRY_OTHER_MODEL = (404, 429, 500, 503)  # retired, rate-limited, or overloaded model


def stream_reply(api_key: str, model: str, context: str, history: list[dict], message: str,
                 temperature: float = 0.7, fallbacks: list[str] | tuple = (),
                 used: dict | None = None) -> Iterator[str]:
    """Stream Gemini's answer. history = [{"role": "user"|"assistant", "content": str}, ...].

    If `model` is retired, rate-limited or overloaded before it starts answering, each model in
    `fallbacks` is tried in turn. `used["model"]` is set to the model that answered.
    """
    from google.genai import errors, types

    contents = [
        types.Content(role="model" if h["role"] == "assistant" else "user",
                      parts=[types.Part.from_text(text=h["content"])])
        for h in history[-MAX_HISTORY_TURNS:]
    ]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
    system = SYSTEM_PROMPT + ("\n\n# DATA CONTEXT\n" + context if context else
                              "\n\n(No dataset selected yet.)")
    config = types.GenerateContentConfig(system_instruction=system, temperature=temperature)
    client = _client(api_key)  # keep a reference: the stream needs the client open

    last_error: Exception | None = None
    for name in [model, *[f for f in fallbacks if f != model]]:
        started = False
        try:
            for chunk in client.models.generate_content_stream(model=name, contents=contents,
                                                               config=config):
                if chunk.text:
                    if not started and used is not None:
                        used["model"] = name
                    started = True
                    yield chunk.text
            if not started:
                yield "_(Gemini returned no text. It may have declined the request; try rephrasing.)_"
            return
        except Exception as exc:
            retry = isinstance(exc, errors.APIError) and exc.code in _RETRY_OTHER_MODEL
            if started and retry:  # cut off mid-answer: keep what arrived so far
                yield ("\n\n_(Gemini stopped mid-answer because the model is overloaded. "
                       "Type **continue** to get the rest, or pick another model.)_")
                return
            if started or not retry:
                raise _friendly(exc) from exc
            last_error = exc
    raise _friendly(last_error) from last_error

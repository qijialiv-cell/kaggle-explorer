"""Descriptive analysis of one data file from a cached Kaggle dataset."""
import altair as alt
import pandas as pd
import streamlit as st

import config
from services import analysis as an
from services import dataset_service as svc
from services import llm
from services.models import AdapterError
from ui import fmt_size, setup_page

setup_page("Analyze", "📈")

MAX_ROWS = 300_000      # rows read for analysis (keeps memory use safe on the cloud)
CHART_SAMPLE = 20_000   # rows sent to the browser for charts


@st.cache_data(max_entries=3, show_spinner="Reading the file…")
def _load(path: str, max_rows: int):
    return an.load_table(path, max_rows)


def _csv_button(df: pd.DataFrame, name: str, key: str) -> None:
    st.download_button("Export CSV", df.to_csv(index=False).encode("utf-8-sig"), file_name=name,
                       mime="text/csv", key=key, icon=":material/download:", disabled=df.empty)


def _chart_rows(df: pd.DataFrame) -> pd.DataFrame:
    return df.sample(CHART_SAMPLE, random_state=0) if len(df) > CHART_SAMPLE else df


st.title("Analyze a dataset")
st.caption("Pick a cached dataset and one of its data files (CSV, Excel, JSON or Parquet). The file is "
           "downloaded from Kaggle once and described here: column types, missing values, summary "
           "statistics, frequencies, correlations and group comparisons.")

# ---- 1. Dataset -------------------------------------------------------------
preset = st.session_state.pop("analyze_id", None)  # set by "Analyze a file" in a detail panel
if preset:
    st.session_state["an_dataset"] = preset

query = st.text_input("Find a dataset", placeholder="Type a keyword, e.g. student performance")
found = svc.search(text=query, limit=50)
labels = dict(zip(found["id"], found["title"] + "  ·  " + found["ref"]))
current = st.session_state.get("an_dataset")
if current and current not in labels:
    ds_cur = svc.get_dataset(current)
    if ds_cur:
        labels = {current: f"{ds_cur['title']}  ·  {ds_cur['ref']}", **labels}
if not labels:
    st.info("No cached datasets match. Try another keyword.")
    st.stop()

dataset_id = st.selectbox("Dataset", list(labels), format_func=labels.get, key="an_dataset")
ds = svc.get_dataset(dataset_id)

# ---- 2. File ----------------------------------------------------------------
if not ds.get("details_fetched_at"):
    st.info("This dataset's file list isn't cached yet.")
    if st.button("Load file list from Kaggle", icon=":material/refresh:"):
        try:
            with st.spinner("Contacting Kaggle…"):
                svc.load_details(dataset_id, force=True)
            st.rerun()
        except AdapterError as exc:
            st.error(str(exc))
    st.stop()

tabular = [f for f in ds["files"] if an.is_tabular(f["name"])]
if not tabular:
    st.info("This dataset has no CSV, Excel, JSON or Parquet files to analyse."
            + (" (Only the first files are cached; check Kaggle.)" if len(ds["files"]) >= svc.MAX_FILES_STORED else ""))
    st.stop()

limit_bytes = svc.MAX_ANALYZE_MB * 1e6


def _file_label(f: dict) -> str:
    too_big = (f["size_bytes"] or 0) > limit_bytes
    return f"{f['name']}  ({fmt_size(f['size_bytes'])}{' · too large' if too_big else ''})"


by_name = {f["name"]: f for f in tabular}
f_col, b_col = st.columns([4, 1], vertical_alignment="bottom")
file_name = f_col.selectbox("File", list(by_name), format_func=lambda n: _file_label(by_name[n]),
                            key=f"an_file_{dataset_id}")
too_big = (by_name[file_name]["size_bytes"] or 0) > limit_bytes
if b_col.button("Download & analyze", type="primary", width="stretch", disabled=too_big,
                icon=":material/analytics:"):
    try:
        with st.spinner(f"Downloading {file_name} from Kaggle…"):
            path = svc.download_for_analysis(dataset_id, file_name)
        st.session_state["an_loaded"] = (dataset_id, file_name, str(path))
    except (AdapterError, ValueError, OSError) as exc:
        st.error(str(exc))
if too_big:
    st.warning(f"Files over {svc.MAX_ANALYZE_MB:g} MB are too large to analyse in the browser app. "
               "Download it from Kaggle and use Python/R locally.")

loaded = st.session_state.get("an_loaded")
if not loaded or loaded[:2] != (dataset_id, file_name):
    st.stop()

try:
    df, truncated = _load(loaded[2], MAX_ROWS)
except FileNotFoundError:
    st.session_state.pop("an_loaded", None)
    st.warning("The downloaded file was cleaned up. Click **Download & analyze** again.")
    st.stop()
except Exception as exc:
    st.error(f"Could not read {file_name}: {exc}")
    st.stop()

if df.empty:
    st.warning("The file has no rows.")
    st.stop()

# ---- 3. Results -------------------------------------------------------------
st.divider()
ov = an.overview(df)
st.subheader(file_name)
if truncated:
    st.caption(f"Large file: statistics use the first {MAX_ROWS:,} rows.")
m = st.columns(4)
m[0].metric("Rows", f"{ov['rows']:,}")
m[1].metric("Columns", f"{ov['columns']:,}")
m[2].metric("Missing cells", f"{ov['missing_pct']:g}%", help=f"{ov['missing_cells']:,} empty cells")
m[3].metric("Duplicate rows", f"{ov['duplicate_rows']:,}")
st.caption(f"{ov['numeric_columns']} numeric · {ov['categorical_columns']} categorical · "
           f"{ov['datetime_columns']} date/time · {ov['text_columns']} text/ID columns")

profile = an.column_profile(df)
num_cols = an.numeric_columns(df)
cat_cols = an.categorical_columns(df)
stem = file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]

t_cols, t_num, t_cat, t_corr, t_grp, t_prev, t_ai = st.tabs(
    ["Columns", "Numeric", "Categorical", "Correlations", "Compare groups", "Preview", "🤖 Ask Gemini"],
    key="an_tabs")

with t_cols:
    st.dataframe(profile, hide_index=True, width="stretch",
                 column_config={
                     "missing_pct": st.column_config.ProgressColumn("Missing %", min_value=0,
                                                                    max_value=100, format="%.1f%%"),
                     "most_common": "Most common value",
                 })
    _csv_button(profile, f"{stem}_columns.csv", "dl_profile")

with t_num:
    summary = an.numeric_summary(df, num_cols)
    if summary.empty:
        st.info("No numeric columns.")
    else:
        st.dataframe(summary.round(3), hide_index=True, width="stretch")
        st.caption("std = standard deviation · iqr = q3 − q1 · outliers_iqr = values beyond 1.5 × IQR "
                   "from the quartiles · skew > 0 means a long right tail.")
        _csv_button(summary, f"{stem}_numeric_summary.csv", "dl_numeric")
        col = st.selectbox("Distribution of", summary["column"], key="an_num_col")
        # Charts use fixed field names: dots/brackets in real column names break Vega-Lite.
        data = _chart_rows(df[[col]].dropna()).set_axis(["x"], axis=1)
        hist = alt.Chart(data).mark_bar().encode(
            x=alt.X("x:Q", bin=alt.Bin(maxbins=40), title=col),
            y=alt.Y("count():Q", title="Count"))
        box = alt.Chart(data).mark_boxplot(extent=1.5).encode(x=alt.X("x:Q", title=col))
        st.altair_chart(hist.properties(height=260), width="stretch")
        st.altair_chart(box.properties(height=70), width="stretch")

with t_cat:
    choices = cat_cols + [c for c in df.columns if c not in cat_cols]
    col = st.selectbox("Column", choices, key="an_cat_col",
                       help="Categorical columns are listed first.")
    freq = an.frequency_table(df, col, top=20)
    left, right = st.columns([2, 3])
    with left:
        st.dataframe(freq, hide_index=True, width="stretch")
        _csv_button(freq, f"{stem}_{col}_frequencies.csv", "dl_freq")
    with right:
        bars = alt.Chart(freq).mark_bar().encode(
            x=alt.X("count:Q", title="Count"),
            y=alt.Y("value:N", sort="-x", title=None),
            tooltip=["value", "count", "percent"])
        st.altair_chart(bars.properties(height=max(200, 22 * len(freq))), width="stretch")

with t_corr:
    method = st.radio("Method", ["pearson", "spearman"], horizontal=True,
                      help="Pearson: linear relationship. Spearman: rank-based, robust to outliers.")
    corr = an.correlations(df, method=method)
    if corr.empty:
        st.info("Needs at least two numeric columns.")
    else:
        long = corr.rename_axis("a").reset_index().melt("a", var_name="b", value_name="r")
        base = alt.Chart(long).encode(x=alt.X("a:N", title=None, sort=list(corr.columns)),
                                      y=alt.Y("b:N", title=None, sort=list(corr.columns)))
        heat = base.mark_rect().encode(
            color=alt.Color("r:Q", scale=alt.Scale(scheme="redblue", domain=[-1, 1]), title="r"),
            tooltip=["a", "b", alt.Tooltip("r:Q", format=".2f")])
        size = max(300, 28 * len(corr))
        layers = heat + base.mark_text(fontSize=10).encode(text=alt.Text("r:Q", format=".2f")) \
            if len(corr) <= 15 else heat
        st.altair_chart(layers.properties(height=size), width="stretch")
        st.markdown("**Strongest pairs**")
        pairs = an.top_correlations(corr)
        st.dataframe(pairs.round(3), hide_index=True, width="stretch")
        _csv_button(corr.round(4).reset_index(names="column"), f"{stem}_{method}_correlations.csv", "dl_corr")

with t_grp:
    if not num_cols or not cat_cols:
        st.info("Needs at least one numeric and one categorical column.")
    else:
        g1, g2 = st.columns(2)
        value = g1.selectbox("Numeric value", num_cols, key="an_grp_val")
        by = g2.selectbox("Group by", [c for c in cat_cols if c != value] or cat_cols, key="an_grp_by")
        grp = an.group_summary(df, value, by)
        st.dataframe(grp.round(3), hide_index=True, width="stretch")
        _csv_button(grp, f"{stem}_{value}_by_{by}.csv", "dl_grp")
        top_groups = grp[by].tolist()[:15]
        data = _chart_rows(df[[value, by]].dropna().astype({by: str})).set_axis(["v", "g"], axis=1)
        data = data[data["g"].isin(top_groups)]
        box = alt.Chart(data).mark_boxplot(extent=1.5).encode(
            x=alt.X("v:Q", title=value), y=alt.Y("g:N", title=by, sort=top_groups))
        st.altair_chart(box.properties(height=max(150, 30 * len(top_groups))), width="stretch")

with t_prev:
    st.dataframe(df.head(200), width="stretch")
    st.caption(f"First 200 of {len(df):,} rows.")

# ---- 4. Gemini research assistant -------------------------------------------
def _gemini_key() -> str | None:
    return st.session_state.get("gemini_key") or config.get_setting("gemini_api_key")


with t_ai:
    if not _gemini_key():
        st.markdown("Ask Google Gemini about this file: research questions, hypotheses, an analysis "
                    "plan, data-quality issues, teaching ideas, or anything else.")
        with st.form("gemini_key_form", border=False):
            pasted = st.text_input("Gemini API key", type="password", placeholder="AIza…",
                                   help="Create one at https://aistudio.google.com/apikey")
            if st.form_submit_button("Use this key", type="primary"):
                try:
                    with st.spinner("Checking the key…"):
                        models = llm.list_models(pasted)
                    st.session_state["gemini_key"] = pasted.strip()
                    st.session_state["gemini_models"] = models
                    st.rerun()
                except llm.GeminiError as exc:
                    st.error(str(exc))
        st.caption("🔒 The key is kept only in your browser session on this app — it is never saved to "
                   "disk or shared with teammates, and is forgotten when you sign out or close the tab.")
    else:
        key = _gemini_key()
        if "gemini_models" not in st.session_state:
            try:
                st.session_state["gemini_models"] = llm.list_models(key)
            except llm.GeminiError as exc:
                st.error(str(exc))
                st.session_state["gemini_models"] = []
        models = st.session_state["gemini_models"] or llm.PREFERRED_MODELS[:1]

        s1, s2, s3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        model = s1.selectbox("Model", models, key="gemini_model",
                             help="Flash models are fast and cheap; Pro models reason more deeply.")
        sample = s2.toggle("Also send the first 20 rows", key="gemini_sample",
                           help="Off: Gemini only sees metadata and summary statistics. Turn on so it "
                                "can see real values (don't use this with personal or sensitive data).")
        if st.session_state.get("gemini_key") and s3.button("Forget key", width="stretch"):
            for k in ("gemini_key", "gemini_models"):
                st.session_state.pop(k, None)
            st.rerun()
        st.caption("Gemini receives the dataset description and the statistics on this page"
                   + (" plus the first 20 rows" if sample else "") + ". Check its answers — it can be wrong.")

        chat_key = f"gemini_chat_{dataset_id}_{file_name}"
        history: list[dict] = st.session_state.setdefault(chat_key, [])

        st.markdown("**Quick start**")
        labels_q = list(llm.QUICK_PROMPTS)
        prompt = None
        for row in (labels_q[:4], labels_q[4:]):
            cols = st.columns(4)
            for col, label in zip(cols, row):
                if col.button(label, key=f"q_{label}", width="stretch"):
                    prompt = llm.QUICK_PROMPTS[label]

        for msg in history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        typed = st.chat_input("Ask anything about this data, or another research topic…", key="gemini_input")
        prompt = prompt or typed
        if prompt:
            with st.chat_message("user"):
                st.markdown(prompt)
            context = llm.build_context(ds, file_name, df, sample_rows=20 if sample else 0,
                                        truncated=truncated)
            with st.chat_message("assistant"):
                used: dict = {}
                try:
                    answer = st.write_stream(llm.stream_reply(
                        key, model, context, history, prompt,
                        fallbacks=[m for m in models if "flash" in m and m != model][:6], used=used))
                except llm.GeminiError as exc:
                    st.error(str(exc))
                    answer = None
            if answer and used.get("model") not in (None, model):
                st.toast(f"{model} was busy, so {used['model']} answered.")
            if answer:
                history += [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": answer if isinstance(answer, str) else "".join(map(str, answer))}]
                st.rerun()

        if history:
            c1, c2 = st.columns(2)
            transcript = f"# Gemini notes: {ds['title']} / {file_name}\n\n" + "\n\n".join(
                f"## {'You' if m['role'] == 'user' else 'Gemini'}\n\n{m['content']}" for m in history)
            c1.download_button("Save conversation (.md)", transcript.encode("utf-8"),
                               file_name=f"{stem}_gemini_notes.md", mime="text/markdown",
                               icon=":material/download:", width="stretch")
            if c2.button("Clear conversation", width="stretch", icon=":material/delete_sweep:"):
                st.session_state[chat_key] = []
                st.rerun()

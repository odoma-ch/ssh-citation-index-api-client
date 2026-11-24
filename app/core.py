"""Shared plumbing for the Streamlit front-end: settings, client, jobs, tables."""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
import streamlit as st

from citation_index_api_client import (
    APIError,
    CitationIndexClient,
    JobResponse,
    LinkedReference,
    is_linkable,
)
from citation_index_api_client.client import API_KEY_ENV, DEFAULT_BASE_URL, LINKING_TARGETS

BASE_URL = os.getenv("CITATION_INDEX_API", DEFAULT_BASE_URL)
POLL_INTERVAL = float(os.getenv("CITATION_INDEX_POLL_INTERVAL", "2"))
JOB_TIMEOUT = float(os.getenv("CITATION_INDEX_JOB_TIMEOUT", "1800"))
HTTP_TIMEOUT = float(os.getenv("CITATION_INDEX_HTTP_TIMEOUT", "120"))
# Soft default for "how many references to link", and a hard ceiling guarding
# the external services behind the linking endpoint.
# Unset means "link every linkable record" (still capped by LINK_LIMIT_MAX).
_limit = os.getenv("CITATION_INDEX_LINKING_LIMIT")
LINK_LIMIT_DEFAULT = int(_limit) if _limit else None
LINK_LIMIT_MAX = int(os.getenv("CITATION_INDEX_LINKING_MAX", "500"))

EXTRACTORS = ["pymupdf", "mineru", "grobid"]
PARSERS = ["llm", "grobid"]
LLM_METHODS = ["full_text", "semantic"]
CLASSIFY_MODES = {
    "M": "M — per citation, uses the section when present",
    "WS": "WS — all citations have sections",
    "WoS": "WoS — ignore sections",
}

# Parsed records vary by parser and document, so tables show these columns first
# when present and append whatever else came back.
PARSED_FIELD_ORDER = [
    "full_title",
    "authors",
    "publication_year",
    "journal_title",
    "container_title",
    "monographic_title",
    "analytic_title",
    "editors",
    "publisher",
    "publication_place",
    "volume",
    "issue",
    "pages",
    "ref_type",
    "identifiers",
]
PARSED_FIELD_SKIP = {"raw"}


# --- connection -------------------------------------------------------------


def client() -> CitationIndexClient:
    """One client per session, rebuilt when the sidebar key changes.

    The URL is deliberately not editable in the UI: the environment key would
    otherwise be sent to whatever host a visitor typed in.
    """
    config = (BASE_URL, api_key())
    cached = st.session_state.get("_client")
    if cached is None or cached[0] != config:
        if cached is not None:
            cached[1].close()
        cached = (config, CitationIndexClient(config[0], api_key=config[1], timeout=HTTP_TIMEOUT))
        st.session_state["_client"] = cached
    return cached[1]


def api_key() -> str | None:
    return st.session_state.get("api_key") or os.getenv(API_KEY_ENV) or None


def short_error(text: str, limit: int = 400) -> str:
    """Upstream services sometimes return a whole HTML error page as the message."""
    cut = text.find("<")
    if cut != -1 and "html" in text[cut : cut + 200].lower():
        text = text[:cut].rstrip(" :") + " (the service returned an HTML error page)"
    return text if len(text) <= limit else text[:limit] + "…"


def describe(error: Exception) -> str:
    if isinstance(error, APIError):
        detail = error.detail
        if isinstance(detail, dict):
            detail = detail.get("detail", detail)
        if error.status_code == 401:
            return "HTTP 401 — the API key is missing or invalid (set it in the sidebar)."
        return f"HTTP {error.status_code} — {detail}" if detail else f"HTTP {error.status_code}"
    return short_error(str(error) or type(error).__name__)


# --- jobs -------------------------------------------------------------------


def jobs() -> dict[str, dict[str, Any]]:
    """Every job this session submitted, keyed by the stage that submitted it."""
    return st.session_state.setdefault("jobs", {})


def duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def result_summary(result: Any) -> str:
    """What a finished job returned, e.g. '16 references · 16 parsed references'."""
    if isinstance(result, list):
        return f"{len(result)} items"
    if not isinstance(result, Mapping):
        return "a result"
    parts = []
    for name, value in result.items():
        if isinstance(value, list):
            # Nested lists (GROBID contexts per reference) count their items.
            count = sum(len(v) for v in value) if value and all(isinstance(v, list) for v in value) else len(value)
            parts.append(f"{count} {name.replace('_', ' ')}")
        elif isinstance(value, str) and len(value) > 80:
            parts.append(f"{len(value):,} characters of {name}")
    return " · ".join(parts) or "a result"


def _show_done(job: dict[str, Any]) -> None:
    took = f" in {duration(job['finished'] - job['submitted'])}" if job.get("finished") else ""
    st.caption(f"Returned {result_summary(job['result'])}{took}. The full results are shown below.")


def run_job(key: str, label: str, submit: Callable[[], JobResponse]) -> Any | None:
    """Run one stage: submit at most once, poll, and cache the result in the session.

    Streamlit reruns the script on any interaction, which aborts a running poll
    loop. Keying jobs by stage means a rerun never resubmits: a finished stage
    returns its cached result, an unfinished one resumes polling its job ID.
    Returns ``None`` if the stage failed; the error stays visible in its status,
    with a button that resubmits only this stage.
    """
    job = jobs().get(key)
    if job and "result" in job:
        with st.status(f"{label} · `{job['job_id']}`", state="complete"):
            _show_done(job)
        return job["result"]
    if job and "error" in job:
        _show_failure(key, label, job["error"])
        return None

    with st.status(label, expanded=False) as status:
        try:
            if job is None:
                job = {"label": label, "job_id": submit().job_id, "submitted": time.time()}
                jobs()[key] = job
            deadline = time.monotonic() + JOB_TIMEOUT
            while True:
                state = client().get_job_status(job["job_id"])
                if state.status == "failed":
                    raise RuntimeError(state.error or "the job failed")
                if state.is_terminal:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"no result after {JOB_TIMEOUT:.0f}s — look it up later on the Jobs page")
                if state.status == "queued":
                    progress = "queued, waiting for a free worker"
                else:
                    progress = state.status + (f" · {state.current_stage}" if state.current_stage else "")
                elapsed = duration(time.time() - job["submitted"])
                status.update(label=f"{label} · `{job['job_id']}` · {progress} · {elapsed}")
                time.sleep(POLL_INTERVAL)
            job["result"] = client().get_job_result(job["job_id"])
            job["finished"] = time.time()
        except Exception as exc:  # noqa: BLE001 — any failure ends this stage only
            failed = job or {"label": label, "job_id": None, "submitted": time.time()}
            failed["error"] = describe(exc)
            jobs()[key] = failed
            status.update(label=f"{label} — failed", state="error", expanded=True)
        if "error" in jobs()[key]:
            _show_failure(key, label, jobs()[key]["error"], status_shown=True)
            return None
        status.update(label=f"{label} · `{job['job_id']}`", state="complete")
        _show_done(job)
    return job["result"]


def _show_failure(key: str, label: str, error: str, *, status_shown: bool = False) -> None:
    if not status_shown:
        st.status(f"{label} — failed", state="error")
    st.error(error)
    if "unavailable" in error.lower() or "503" in error:
        st.caption("The service behind this step is busy or down; retrying in a minute usually works.")
    if st.button("Retry this step", key=f"retry-{key}", icon=":material/refresh:"):
        # Archive the failed attempt under a new key so it stays listed on Jobs.
        jobs()[f"{key}:failed-{time.time_ns()}"] = jobs().pop(key)
        st.rerun()


def run_failures(run_id: str) -> list[dict[str, Any]]:
    """Failed stages of the current attempt of a run (retried ones excluded)."""
    return [
        job
        for key, job in jobs().items()
        if key.startswith(f"{run_id}:") and ":failed-" not in key and "error" in job
    ]


def new_run(prefix: str, **params: Any) -> None:
    """Start a fresh run of a workflow; its stages get new job keys."""
    st.session_state[f"run:{prefix}"] = {"id": f"{prefix}-{time.time_ns()}", **params}


def current_run(prefix: str) -> dict[str, Any] | None:
    return st.session_state.get(f"run:{prefix}")


def handoff(page: str, key: str, value: Any) -> None:
    """Prefill another workflow's input and switch to it."""
    st.session_state[key] = value
    st.switch_page(page)


# --- uploads ----------------------------------------------------------------


def upload_payload(upload: Any) -> dict[str, Any] | None:
    """Copy an uploaded file into the run, so reruns can resubmit without the widget."""
    if upload is None:
        return None
    return {"name": upload.name, "data": upload.getvalue()}


def as_file(payload: Mapping[str, Any]) -> io.BytesIO:
    return io.BytesIO(payload["data"])


def file_stem(name: str | None) -> str:
    return re.sub(r"[^\w.-]+", "_", Path(name or "document").stem)[:40] or "document"


# --- tables -----------------------------------------------------------------


def _format_mapping(value: Mapping[str, Any]) -> str:
    """Render the nested records the parser returns — people, identifiers — as text."""
    for key in ("full_name", "literal"):
        if value.get(key):
            return str(value[key])
    parts = [
        value.get("first_name") or value.get("forename") or value.get("name"),
        value.get("middle_name"),
        value.get("name_link"),
        value.get("surname"),
    ]
    joined = " ".join(str(part) for part in parts if part)
    if joined:
        return joined
    if value.get("value"):
        kind = value.get("type") or value.get("scheme")
        return f"{kind}:{value['value']}" if kind else str(value["value"])
    return json.dumps(value, ensure_ascii=False)


def format_value(value: Any) -> Any:
    """Flatten nested API output into something a table cell can show."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _format_mapping(value)
    if isinstance(value, list):
        return "; ".join(str(format_value(item)) for item in value if item is not None)
    return str(value)


def records_frame(records: Sequence[Mapping[str, Any] | None]) -> pd.DataFrame:
    """Parsed records as a table; ``#`` is the 1-based position in the input."""
    seen: list[str] = []
    for record in records:
        for key in record or {}:
            if key not in seen and key not in PARSED_FIELD_SKIP:
                seen.append(key)
    ordered = [key for key in PARSED_FIELD_ORDER if key in seen]
    ordered += [key for key in seen if key not in ordered]
    rows = [
        {"#": i, **{key: format_value((record or {}).get(key)) for key in ordered}}
        for i, record in enumerate(records, start=1)
    ]
    return pd.DataFrame(rows, columns=["#", *ordered])


def strings_frame(strings: Iterable[str], column: str = "reference") -> pd.DataFrame:
    return pd.DataFrame(
        [{"#": i, column: s} for i, s in enumerate(strings, start=1)], columns=["#", column]
    )


def reference_label(reference: Any) -> str:
    if isinstance(reference, Mapping):
        for key in ("full_title", "analytic_title", "monographic_title", "container_title", "journal_title"):
            if reference.get(key):
                return str(reference[key])
        return json.dumps(reference, ensure_ascii=False)
    return str(reference)


def links_frame(linked: Sequence[LinkedReference], numbers: Sequence[int] | None = None) -> pd.DataFrame:
    """One row per reference, one column per target holding the matched ID."""
    targets: list[str] = []
    for item in linked:
        targets += [t for t in item.links if t not in targets]
    rows = []
    for i, item in enumerate(linked):
        row: dict[str, Any] = {
            "#": numbers[i] if numbers else i + 1,
            "title": reference_label(item.reference),
        }
        for target in targets:
            link = item.links.get(target)
            row[target] = (link.id or "") if link else ""
            row[f"{target} DOI"] = (link.doi or "") if link else ""
            if link and link.error:
                row[f"{target} error"] = link.error
        rows.append(row)
    return pd.DataFrame(rows).fillna("")


def link_stats(linked: Sequence[LinkedReference]) -> tuple[int, int]:
    """(matched lookups, total lookups) over every reference × target."""
    hits = sum(link.is_linked for item in linked for link in item.links.values())
    return hits, sum(len(item.links) for item in linked)


def show_table(
    frame: pd.DataFrame, name: str, *, key: str, column_config: Mapping[str, Any] | None = None
) -> None:
    """A read-only table with a CSV download that doesn't rerun the page."""
    st.dataframe(frame, hide_index=True, width="stretch", column_config=column_config)
    st.download_button(
        "Download CSV",
        frame.to_csv(index=False).encode("utf-8"),
        file_name=f"{name}.csv",
        mime="text/csv",
        key=f"csv-{key}",
        on_click="ignore",
    )


def json_download(data: Any, name: str, *, key: str, label: str = "Download JSON") -> None:
    st.download_button(
        label,
        json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        file_name=f"{name}.json",
        mime="application/json",
        key=f"json-{key}",
        on_click="ignore",
    )


# --- citation intents -------------------------------------------------------

# Ensemble confidences in the classifier's result, renamed for display.
INTENT_CONFIDENCE = {
    "MET ENSEMBLE CONFIDENCE": "method",
    "BKG ENSEMBLE CONFIDENCE": "background",
    "RES ENSEMBLE CONFIDENCE": "result",
}


def flatten_contexts(contexts: Sequence[Any] | None) -> list[dict[str, Any]]:
    """GROBID gives one list of citing sentences per reference; flatten them.

    Each sentence keeps its 1-based ``reference`` number so intents can be joined
    back. Empty sentences are dropped: the classifier rejects the whole batch.
    """
    return [
        {**context, "reference": number}
        for number, group in enumerate(contexts or [], start=1)
        for context in (group if isinstance(group, list) else [group])
        if isinstance(context, Mapping) and str(context.get("citation") or "").strip()
    ]


def classifier_input(citations: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"section": str(c.get("section") or ""), "citation": str(c["citation"]).strip()}
        for c in citations
    ]


def intent_label(prediction: Any) -> str:
    """'usesMethodIn (METHOD)' -> 'method'."""
    match = re.search(r"\(([^)]+)\)", str(prediction or ""))
    return (match.group(1) if match else str(prediction or "")).lower()


def intents_frame(
    citations: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]
) -> pd.DataFrame:
    """Join classifier results back onto the submitted sentences (same order)."""
    rows = []
    for i, (citation, result) in enumerate(zip(citations, results), start=1):
        row: dict[str, Any] = {"#": citation.get("reference", i)}
        if citation.get("marker"):
            row["marker"] = citation["marker"]
        row["section"] = citation.get("section") or ""
        row["citation"] = citation.get("citation") or ""
        row["intent"] = intent_label(result.get("FINAL PREDICTION"))
        for source, name in INTENT_CONFIDENCE.items():
            if source in result:
                row[name] = round(float(result[source]), 3)
        rows.append(row)
    return pd.DataFrame(rows)


def intent_summary(intents: pd.DataFrame) -> dict[int, str]:
    """Per reference number, its intents as 'background ×2, method'."""
    if intents.empty:
        return {}
    summary = {}
    for number, group in intents.groupby("#"):
        counts = group["intent"].value_counts()
        summary[int(number)] = ", ".join(
            f"{label} ×{n}" if n > 1 else label for label, n in counts.items()
        )
    return summary


# --- shared stages ----------------------------------------------------------


def link_options(key: str) -> tuple[list[str], int]:
    """Target and batch-size inputs, shared by every form that links."""
    targets = st.multiselect(
        "Linking targets",
        list(LINKING_TARGETS),
        default=["matilda"],
        key=f"{key}-targets",
        help="Each reference is looked up live in every target, so more targets is slower.",
    )
    limit = st.number_input(
        "Max references to link",
        min_value=1,
        max_value=LINK_LIMIT_MAX,
        value=min(LINK_LIMIT_DEFAULT, LINK_LIMIT_MAX) if LINK_LIMIT_DEFAULT else None,
        placeholder=f"All linkable (up to {LINK_LIMIT_MAX})",
        key=f"{key}-limit",
        help=f"Leave empty to link every linkable record in one batch. Ceiling {LINK_LIMIT_MAX} (CITATION_INDEX_LINKING_MAX).",
    )
    return targets, int(limit) if limit else LINK_LIMIT_MAX


def link_stage(
    key: str, records: Sequence[Mapping[str, Any] | None], targets: Sequence[str], limit: int
) -> tuple[list[LinkedReference], list[int]]:
    """Link the linkable records; returns results and their 1-based record numbers."""
    numbered = [(i, r) for i, r in enumerate(records, start=1) if r and is_linkable(r)]
    skipped = len(records) - len(numbered)
    if skipped:
        st.caption(
            f"{skipped} of {len(records)} records skipped — linking needs a title, "
            "authors and a publication year, and rejects the whole batch otherwise."
        )
    chosen = numbered[:limit]
    if not chosen:
        st.warning("No record has the title, authors and year that linking needs.")
        return [], []
    if len(chosen) == len(numbered):
        what = f"Linking all {len(chosen)} linkable records"
    else:
        what = f"Linking the first {len(chosen)} of {len(numbered)} linkable records (limit {limit})"
    result = run_job(
        key,
        f"{what} → {', '.join(targets)} in one batch "
        f"({len(chosen) * len(targets)} live lookups, can take minutes)",
        lambda: client().link_references([r for _, r in chosen], target=list(targets)),
    )
    if result is None:
        return [], []
    linked = [LinkedReference.from_dict(item) for item in result.get("results", [])]
    return linked, [i for i, _ in chosen]


def show_intents(intents: pd.DataFrame, name: str, *, key: str) -> None:
    counts = intents["intent"].value_counts()
    # Always show the three classes, even at 0; append any label the API adds.
    labels = list(INTENT_CONFIDENCE.values())
    labels += [label for label in counts.index if label not in labels]
    for column, label in zip(st.columns(len(labels)), labels):
        column.metric(label, int(counts.get(label, 0)), border=True)
    show_table(intents, name, key=key)


def classify_stage(
    key: str, citations: Sequence[Mapping[str, Any]], mode: str
) -> tuple[pd.DataFrame | None, Any]:
    """Classify citing sentences; returns (intents table, raw result)."""
    result = run_job(
        key,
        f"Classifying {len(citations)} citing sentences (mode {mode})",
        lambda: client().classify_citations(classifier_input(citations), mode=mode),
    )
    if result is None:
        return None, None
    return intents_frame(citations, result.get("results", [])), result


def show_links(linked: Sequence[LinkedReference], numbers: Sequence[int], name: str, *, key: str) -> None:
    hits, lookups = link_stats(linked)
    st.caption(
        f"{hits} of {lookups} lookups matched. An empty cell means the target found "
        "no confident match (it needs a DOI hit or a near-exact title); a failed "
        "lookup shows its message in the target's error column."
    )
    frame = links_frame(linked, numbers)
    # Target IDs are URLs (OpenAlex, Wikidata, …); make them clickable.
    urls = {
        column: st.column_config.LinkColumn(column)
        for column in frame.columns
        if frame[column].astype(str).str.startswith("http").any()
    }
    show_table(frame, name, key=key, column_config=urls)

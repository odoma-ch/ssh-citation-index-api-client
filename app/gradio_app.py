"""Read-only Gradio front-end for the Citation Index API."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

import gradio as gr
import pandas as pd

from citation_index_api_client import (
    APIError,
    CitationIndexClient,
    LinkedReference,
    is_linkable,
)
from citation_index_api_client.client import DEFAULT_BASE_URL, LINKING_TARGETS

BASE_URL = os.getenv("CITATION_INDEX_API", DEFAULT_BASE_URL)
POLL_INTERVAL = float(os.getenv("CITATION_INDEX_POLL_INTERVAL", "2"))
JOB_TIMEOUT = float(os.getenv("CITATION_INDEX_JOB_TIMEOUT", "1800"))
HTTP_TIMEOUT = float(os.getenv("CITATION_INDEX_HTTP_TIMEOUT", "120"))

# Soft default for the "how many references to link" slider, and an absolute
# ceiling that guards the external services behind the linking endpoint.
LINK_LIMIT_DEFAULT = int(os.getenv("CITATION_INDEX_LINKING_LIMIT", "25"))
LINK_LIMIT_MAX = int(os.getenv("CITATION_INDEX_LINKING_MAX", "500"))

EXTRACTORS = ["mineru", "pymupdf"]
PARSERS = ["llm", "grobid"]

PDF_SOURCE = "Upload a PDF"
MARKDOWN_SOURCE = "Paste Markdown"

REFERENCE_HEADERS = ["#", "reference"]
LINK_HEADERS = ["#", "reference", "target", "id", "doi"]

# Parsed records vary by parser and document, so the table shows these columns
# first when present and appends whatever else the parser returned.
PARSED_FIELD_ORDER = [
    "analytic_title",
    "monographic_title",
    "full_title",
    "journal_title",
    "authors",
    "editors",
    "publisher",
    "publication_year",
    "publication_date",
    "publication_place",
    "volume",
    "issue",
    "pages",
    "ref_type",
    "identifiers",
]
PARSED_FIELD_SKIP = {"raw"}

# Gradio only serves files from paths it knows about; this directory is passed
# to launch(allowed_paths=...) so generated CSVs are downloadable.
DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="citation-index-ui-"))

client = CitationIndexClient(base_url=BASE_URL, timeout=HTTP_TIMEOUT)


def _describe(error: APIError) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        detail = detail.get("detail", detail)
    return f"HTTP {error.status_code} — {detail}" if detail else f"HTTP {error.status_code}"


def _poll(job_id: str, *, label: str) -> Iterator[str]:
    """Yield a status line per poll until the job completes, or raise gr.Error."""
    deadline = time.monotonic() + JOB_TIMEOUT
    while True:
        try:
            status = client.get_job_status(job_id)
        except APIError as exc:
            raise gr.Error(f"{label} failed: {_describe(exc)}") from exc
        if status.is_terminal:
            if status.status == "failed":
                raise gr.Error(f"{label} failed: {status.error or 'unknown error'}")
            return
        if time.monotonic() >= deadline:
            raise gr.Error(f"{label} timed out after {JOB_TIMEOUT:.0f}s")
        stage = f" ({status.current_stage})" if status.current_stage else ""
        yield f"{label}… {status.status}{stage}"
        time.sleep(POLL_INTERVAL)


def _result(job_id: str, *, label: str) -> Any:
    try:
        return client.get_job_result(job_id)
    except APIError as exc:
        raise gr.Error(f"Could not read {label} result: {_describe(exc)}") from exc


def _download_path(name: str) -> Path:
    # A fresh directory per call keeps concurrent sessions from overwriting
    # each other's downloads while preserving a readable file name.
    return Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR)) / name


def _write_csv(name: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    path = _download_path(name)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)
    return str(path)


def _reference_rows(references: Sequence[str]) -> list[list[Any]]:
    return [[i, reference] for i, reference in enumerate(references, start=1)]


def _reference_label(reference: Any) -> str:
    """A one-line label for a linking record (its title), for table display."""
    if isinstance(reference, dict):
        for key in ("full_title", "analytic_title", "monographic_title", "journal_title"):
            if reference.get(key):
                return str(reference[key])
        return json.dumps(reference, ensure_ascii=False)
    return str(reference)


def _link_rows(linked: Sequence[LinkedReference]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for i, item in enumerate(linked, start=1):
        label = _reference_label(item.reference)
        for target, link in item.links.items():
            rows.append([i, label, target, link.id or "", link.doi or ""])
    return rows


def _format_mapping(value: dict[str, Any]) -> str:
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


def _format_value(value: Any) -> Any:
    """Flatten nested parser output into something a table cell can show."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return _format_mapping(value)
    if isinstance(value, list):
        return "; ".join(str(_format_value(item)) for item in value if item is not None)
    return str(value)


def _parsed_frame(records: Sequence[dict[str, Any]]) -> pd.DataFrame:
    seen: list[str] = []
    for record in records:
        for key in record:
            if key not in seen and key not in PARSED_FIELD_SKIP:
                seen.append(key)
    ordered = [key for key in PARSED_FIELD_ORDER if key in seen]
    ordered += [key for key in seen if key not in ordered]

    rows = [
        {"#": i, **{key: _format_value(record.get(key)) for key in ordered}}
        for i, record in enumerate(records, start=1)
    ]
    return pd.DataFrame(rows, columns=["#", *ordered])


def _write_frame_csv(name: str, frame: pd.DataFrame) -> str:
    path = _download_path(name)
    frame.to_csv(path, index=False)
    return str(path)


def _parsed_table_update(frame: pd.DataFrame | None) -> dict[str, Any]:
    """Keep text columns readable without squeezing the remaining headers."""
    widths = []
    if frame is not None:
        for field in frame.columns:
            if field == "#":
                widths.append(56)
            elif "title" in field:
                widths.append(320)
            elif field in ("authors", "editors"):
                widths.append(260)
            else:
                widths.append(180)
    return gr.update(value=frame, column_widths=widths)


def _raw_links(linked: Sequence[LinkedReference]) -> dict[str, Any]:
    return {
        "results": [
            {
                "reference": item.reference,
                "links": {
                    target: {"id": link.id, "doi": link.doi}
                    for target, link in item.links.items()
                },
            }
            for item in linked
        ]
    }


def _checked_targets(targets: Sequence[str]) -> list[str]:
    if not targets:
        raise gr.Error("Select at least one linking target.")
    return list(targets)


def _link(records: Sequence[dict[str, Any]], targets: Sequence[str], label: str):
    """Submit a linking batch of parsed records; yield status, then the results."""
    try:
        job = client.link_references(list(records), target=list(targets))
    except APIError as exc:
        raise gr.Error(f"{label} could not be submitted: {_describe(exc)}") from exc
    yield f"{label}… queued as {job.job_id}", None
    for update in _poll(job.job_id, label=label):
        yield update, None
    result = _result(job.job_id, label=label.lower())
    linked = [LinkedReference.from_dict(item) for item in result.get("results", [])]
    yield None, linked


def run_pipeline(
    source: str,
    pdf_path: str | None,
    markdown_text: str | None,
    extractor: str,
    parser: str,
    targets: Sequence[str],
    link_limit: int,
    link_all: bool,
):
    """Extract, parse and link references from a PDF or from pasted Markdown."""
    from_pdf = source == PDF_SOURCE
    if from_pdf and not pdf_path:
        raise gr.Error("Upload a PDF, or switch the input to Markdown.")
    if not from_pdf and not (markdown_text or "").strip():
        raise gr.Error("Paste some Markdown, or switch the input to PDF.")
    chosen = _checked_targets(targets)

    state: dict[str, Any] = {
        "status": "",
        "text": None,
        "references": None,
        "parsed": None,
        "links": None,
        "references_csv": None,
        "parsed_csv": None,
        "links_csv": None,
        "raw": None,
    }

    def snapshot() -> tuple[Any, ...]:
        return (
            state["status"],
            state["text"],
            state["references"],
            _parsed_table_update(state["parsed"]),
            state["links"],
            state["references_csv"],
            state["parsed_csv"],
            state["links_csv"],
            state["raw"],
        )

    # Any stage may fail (a job errors, the API is down). Catch it here, report
    # it in the status line and yield the snapshot so far — never let the
    # exception abort the generator, which would discard the partial results
    # (references, CSVs) already produced by earlier stages.
    try:
        if from_pdf:
            stem = re.sub(r"[^\w.-]+", "_", Path(pdf_path).stem)[:40] or "document"
            try:
                job = client.extract_text(pdf_path, extractor=extractor, markdown=True)
            except APIError as exc:
                raise gr.Error(
                    f"Text extraction could not be submitted: {_describe(exc)}"
                ) from exc
            state["status"] = f"Text extraction… queued as {job.job_id}"
            yield snapshot()
            for update in _poll(job.job_id, label="Extracting text"):
                state["status"] = update
                yield snapshot()

            text = _result(job.job_id, label="text extraction").get("text") or ""
            if not text.strip():
                raise gr.Error("Text extraction returned an empty document.")
        else:
            stem = "pasted-markdown"
            text = markdown_text or ""

        state["text"] = text
        state["status"] = f"{len(text):,} characters of text. Finding references…"
        yield snapshot()

        try:
            job = client.extract_references(text)
        except APIError as exc:
            raise gr.Error(
                f"Reference extraction could not be submitted: {_describe(exc)}"
            ) from exc
        for update in _poll(job.job_id, label="Extracting references"):
            state["status"] = update
            yield snapshot()

        references = (
            _result(job.job_id, label="reference extraction").get("references") or []
        )
        if not references:
            raise gr.Error("No references were found in this document.")
        state["references"] = _reference_rows(references)
        state["references_csv"] = _write_csv(
            f"{stem}-references.csv", REFERENCE_HEADERS, state["references"]
        )
        state["status"] = f"Found {len(references)} references. Parsing with {parser}…"
        yield snapshot()

        try:
            job = client.parse_references(list(references), parser=parser)
        except APIError as exc:
            raise gr.Error(
                f"Reference parsing could not be submitted: {_describe(exc)}"
            ) from exc
        for update in _poll(job.job_id, label=f"Parsing references ({parser})"):
            state["status"] = update
            yield snapshot()

        parsed = _result(job.job_id, label="reference parsing").get("references") or []
        if parsed:
            state["parsed"] = _parsed_frame(parsed)
            state["parsed_csv"] = _write_frame_csv(
                f"{stem}-parsed.csv", state["parsed"]
            )
        if not parsed:
            raise gr.Error(
                "Parsing produced no structured records, so there is nothing to link."
            )

        # Linking consumes the structured parsed records, but the endpoint
        # rejects the whole batch if any record lacks a title, authors or year —
        # which the parser routinely leaves null — so drop those first.
        linkable = [record for record in parsed if is_linkable(record)]
        skipped = len(parsed) - len(linkable)
        if not linkable:
            raise gr.Error(
                f"None of the {len(parsed)} parsed records have the title, authors "
                "and year that linking requires."
            )
        to_link = linkable if link_all else linkable[: max(1, int(link_limit))]
        skipped_note = f" ({skipped} skipped, missing title/authors/year)" if skipped else ""
        state["status"] = (
            f"Parsed {len(parsed)} references. Linking {len(to_link)} of "
            f"{len(linkable)} linkable{skipped_note}…"
        )
        yield snapshot()

        linked: list[LinkedReference] = []
        for update, result in _link(to_link, chosen, "Linking references"):
            if update is not None:
                state["status"] = update
                yield snapshot()
            if result is not None:
                linked = result

        state["links"] = _link_rows(linked)
        state["links_csv"] = _write_csv(
            f"{stem}-links.csv", LINK_HEADERS, state["links"]
        )
        state["raw"] = _raw_links(linked)
        hits = sum(link.is_linked for item in linked for link in item.links.values())
        lookups = sum(len(item.links) for item in linked)
        state["status"] = (
            f"Done. {len(references)} references extracted, {len(parsed)} parsed, "
            f"{len(linked)} linked, {hits}/{lookups} target lookups matched."
        )
        yield snapshot()
    except gr.Error as exc:
        # Keep whatever earlier stages produced; report where it stopped.
        state["status"] = f"Stopped: {exc}"
        yield snapshot()


def run_linking(raw_text: str, targets: Sequence[str], parser: str = "llm"):
    """Link reference strings pasted directly into the UI, one per line."""
    references = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    if not references:
        raise gr.Error("Paste at least one reference, one per line.")
    if len(references) > LINK_LIMIT_MAX:
        raise gr.Error(
            f"{len(references)} references submitted but the ceiling is "
            f"{LINK_LIMIT_MAX}. Linking calls external services once per reference "
            "per target; raise CITATION_INDEX_LINKING_MAX to allow more."
        )
    chosen = _checked_targets(targets)

    parsed_frame = None
    parsed_csv = None

    def snapshot(status, rows=None, links_csv=None, raw=None):
        return status, _parsed_table_update(parsed_frame), rows, parsed_csv, links_csv, raw

    # Linking consumes structured records, so pasted strings are parsed first.
    yield snapshot(f"Parsing {len(references)} references with {parser}…")
    try:
        try:
            job = client.parse_references(references, parser=parser)
        except APIError as exc:
            raise gr.Error(
                f"Reference parsing could not be submitted: {_describe(exc)}"
            ) from exc
        for update in _poll(job.job_id, label=f"Parsing references ({parser})"):
            yield snapshot(update)
        records = _result(job.job_id, label="reference parsing").get("references") or []
        parsed_frame = _parsed_frame(records)
        parsed_csv = _write_frame_csv("parsed-references.csv", parsed_frame)
        yield snapshot(f"Parsed {len(records)} references. Preparing links…")
        records = [record for record in records if is_linkable(record)]
        if not records:
            raise gr.Error(
                "None of the pasted references parsed into a record with the title, "
                "authors and year that linking requires."
            )

        linked: list[LinkedReference] = []
        for update, result in _link(records, chosen, "Linking references"):
            if update is not None:
                yield snapshot(update)
            if result is not None:
                linked = result

        rows = _link_rows(linked)
        hits = sum(link.is_linked for item in linked for link in item.links.values())
        yield snapshot(
            f"Done. {hits}/{len(rows)} target lookups matched.",
            rows,
            _write_csv("linked-references.csv", LINK_HEADERS, rows),
            _raw_links(linked),
        )
    except gr.Error as exc:
        yield snapshot(f"Stopped: {exc}")


def lookup_job(job_id: str):
    """Show the status and, when available, the result of an existing job."""
    job_id = (job_id or "").strip()
    if not job_id:
        raise gr.Error("Paste a job ID.")
    try:
        status = client.get_job_status(job_id)
    except APIError as exc:
        raise gr.Error(f"Could not read job {job_id}: {_describe(exc)}") from exc

    summary = f"{status.status}"
    if status.current_stage:
        summary += f" (stage: {status.current_stage})"
    if status.error:
        summary += f" — {status.error}"

    status_payload = {
        "job_id": status.job_id,
        "status": status.status,
        "created_at": status.created_at,
        "started_at": status.started_at,
        "completed_at": status.completed_at,
        "current_stage": status.current_stage,
        "error": status.error,
        "progress": status.progress,
    }
    result = _result(job_id, label="job") if status.status == "completed" else None
    return summary, status_payload, result


def build_demo() -> gr.Blocks:
    with gr.Blocks(title="SSH Citation Index API – demo") as demo:
        gr.Markdown(
            "# SSH Citation Index API – demo\n"
            "Extract references from a document, parse them into structured records, "
            "and link them to OpenAlex, Matilda and Wikidata. Results are read-only; "
            "use the download buttons to keep a copy.\n\n"
            f"Connected to `{BASE_URL}`."
        )

        with gr.Tab("From a document"):
            gr.Markdown(
                "Runs the full pipeline. Upload a PDF, or paste Markdown you already "
                "have to skip text extraction. Extraction with MinerU can take several "
                "minutes — the status line updates while it runs, and the job ID is "
                "shown in case you need to close the tab and come back."
            )
            with gr.Row():
                with gr.Column(scale=1):
                    source_input = gr.Radio(
                        label="Input",
                        choices=[PDF_SOURCE, MARKDOWN_SOURCE],
                        value=PDF_SOURCE,
                    )
                    pdf_input = gr.File(
                        label="PDF", file_types=[".pdf"], type="filepath"
                    )
                    extractor_input = gr.Dropdown(
                        label="Extractor",
                        choices=EXTRACTORS,
                        value="mineru",
                    )
                    markdown_input = gr.Textbox(
                        label="Markdown",
                        lines=12,
                        visible=False,
                        placeholder="Paste the document text, including its bibliography…",
                    )
                    parser_input = gr.Dropdown(
                        label="Parser",
                        choices=PARSERS,
                        value="llm",
                        info="How raw reference strings become structured records.",
                    )
                    pipeline_targets = gr.CheckboxGroup(
                        label="Linking targets",
                        choices=list(LINKING_TARGETS),
                        value=["openalex"],
                    )
                    link_all_input = gr.Checkbox(
                        label="Link every extracted reference",
                        value=False,
                        info="Slower: each reference is looked up in every target.",
                    )
                    link_limit = gr.Slider(
                        label="References to link",
                        minimum=1,
                        maximum=LINK_LIMIT_MAX,
                        value=min(LINK_LIMIT_DEFAULT, LINK_LIMIT_MAX),
                        step=1,
                    )
                    pipeline_button = gr.Button("Run", variant="primary")
                with gr.Column(scale=2):
                    pipeline_status = gr.Textbox(
                        label="Status", interactive=False, lines=2
                    )
                    pipeline_references = gr.Dataframe(
                        label="References",
                        headers=REFERENCE_HEADERS,
                        interactive=False,
                        wrap=True,
                    )
                    pipeline_parsed = gr.Dataframe(
                        label="Parsed references",
                        headers=["#"],
                        interactive=False,
                        wrap=True,
                    )
                    pipeline_links = gr.Dataframe(
                        label="Links",
                        headers=LINK_HEADERS,
                        interactive=False,
                        wrap=True,
                    )
                    with gr.Row():
                        pipeline_references_csv = gr.File(
                            label="References (CSV)", interactive=False
                        )
                        pipeline_parsed_csv = gr.File(
                            label="Parsed references (CSV)", interactive=False
                        )
                        pipeline_links_csv = gr.File(
                            label="Links (CSV)", interactive=False
                        )
                    with gr.Accordion("Document text", open=False):
                        pipeline_text = gr.Textbox(
                            label="Markdown", interactive=False, lines=20, max_lines=20
                        )
                    with gr.Accordion("Raw linking response", open=False):
                        pipeline_raw = gr.JSON(label="JSON")

            def toggle_source(source: str):
                from_pdf = source == PDF_SOURCE
                return (
                    gr.update(visible=from_pdf),
                    gr.update(visible=from_pdf),
                    gr.update(visible=not from_pdf),
                )

            source_input.change(
                toggle_source,
                inputs=[source_input],
                outputs=[pdf_input, extractor_input, markdown_input],
            )
            link_all_input.change(
                lambda link_all: gr.update(interactive=not link_all),
                inputs=[link_all_input],
                outputs=[link_limit],
            )

            pipeline_button.click(
                run_pipeline,
                inputs=[
                    source_input,
                    pdf_input,
                    markdown_input,
                    extractor_input,
                    parser_input,
                    pipeline_targets,
                    link_limit,
                    link_all_input,
                ],
                outputs=[
                    pipeline_status,
                    pipeline_text,
                    pipeline_references,
                    pipeline_parsed,
                    pipeline_links,
                    pipeline_references_csv,
                    pipeline_parsed_csv,
                    pipeline_links_csv,
                    pipeline_raw,
                ],
                concurrency_limit=2,
            )

        with gr.Tab("From reference strings"):
            gr.Markdown(
                "Paste references, one per line, to link them without a document. "
                "Each is parsed into a structured record first (the linking API no "
                "longer accepts raw strings), then linked. An empty ID and DOI means "
                "the target found no confident match."
            )
            with gr.Row():
                with gr.Column(scale=1):
                    linking_input = gr.Textbox(
                        label="References (one per line)",
                        lines=12,
                        placeholder="Smith, J. (2020). Title. Journal. doi:10.1234/abc",
                    )
                    linking_parser = gr.Dropdown(
                        label="Parser",
                        choices=PARSERS,
                        value="llm",
                        info="How raw reference strings become structured records.",
                    )
                    linking_targets = gr.CheckboxGroup(
                        label="Linking targets",
                        choices=list(LINKING_TARGETS),
                        value=["openalex"],
                    )
                    linking_button = gr.Button("Link", variant="primary")
                with gr.Column(scale=2):
                    linking_status = gr.Textbox(
                        label="Status", interactive=False, lines=2
                    )
                    linking_parsed = gr.Dataframe(
                        label="Parsed references",
                        headers=["#"],
                        interactive=False,
                        wrap=True,
                    )
                    linking_table = gr.Dataframe(
                        label="Links",
                        headers=LINK_HEADERS,
                        interactive=False,
                        wrap=True,
                    )
                    with gr.Row():
                        linking_parsed_csv = gr.File(
                            label="Parsed references (CSV)", interactive=False
                        )
                        linking_csv = gr.File(label="Links (CSV)", interactive=False)
                    with gr.Accordion("Raw response", open=False):
                        linking_raw = gr.JSON(label="JSON")

            linking_button.click(
                run_linking,
                inputs=[linking_input, linking_targets, linking_parser],
                outputs=[
                    linking_status, linking_parsed, linking_table,
                    linking_parsed_csv, linking_csv, linking_raw,
                ],
                concurrency_limit=2,
            )

        with gr.Tab("Look up a job"):
            gr.Markdown(
                "Jobs keep running after you close the browser. Paste a job ID to see "
                "its status and, once it has finished, its result."
            )
            with gr.Row():
                job_input = gr.Textbox(label="Job ID", scale=3)
                job_button = gr.Button("Fetch", variant="primary", scale=1)
            job_summary = gr.Textbox(label="Status", interactive=False)
            with gr.Accordion("Status detail", open=False):
                job_status = gr.JSON(label="JSON")
            job_result = gr.JSON(label="Result")

            job_button.click(
                lookup_job,
                inputs=[job_input],
                outputs=[job_summary, job_status, job_result],
            )

    return demo


def main() -> None:
    credentials = os.getenv("CITATION_INDEX_UI_AUTH")
    auth = tuple(credentials.split(":", 1)) if credentials else None
    build_demo().launch(
        server_name=os.getenv("CITATION_INDEX_UI_HOST")
        or os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.getenv("CITATION_INDEX_UI_PORT", "7860")),
        auth=auth,
        allowed_paths=[str(DOWNLOAD_DIR)],
        max_file_size=os.getenv("CITATION_INDEX_UI_MAX_UPLOAD", "50mb"),
    )


if __name__ == "__main__":
    main()

"""Single step: link references to OpenAlex, Matilda, Wikidata and OpenCitations."""

from __future__ import annotations

import json

import streamlit as st

import core

SOURCES = {"strings": "Reference strings (parsed first)", "records": "Parsed records (JSON)"}

st.title("Link")
st.caption("Find each reference in external scholarly indexes.")

# Other pages hand over parsed records here before switching to this page.
prefill = st.session_state.pop("link-prefill", None)
if prefill is not None:
    st.session_state["link-records"] = prefill
    st.session_state["link-source"] = "records"
st.session_state.setdefault("link-source", "strings")
source = st.segmented_control("Input", list(SOURCES), format_func=SOURCES.get, key="link-source")

with st.form("link-form"):
    if source == "records":
        raw = st.text_area(
            "Parsed records — a JSON list, as returned by Parse",
            height=240,
            key="link-records",
        )
        uploaded = st.file_uploader("…or upload a JSON file", type="json")
        parser = None
    else:
        raw = st.text_area("References, one per line", height=240, key="link-strings")
        uploaded = None
        parser = st.selectbox("Parser", core.PARSERS)
    targets, limit = core.link_options("link")
    if st.form_submit_button("Link", type="primary"):
        if not targets:
            st.error("Pick at least one target.")
        elif source == "records":
            try:
                records = json.loads(uploaded.getvalue() if uploaded else raw or "null")
                records = records.get("references", records) if isinstance(records, dict) else records
                if not isinstance(records, list) or not records:
                    raise ValueError("expected a non-empty JSON list of records")
                core.new_run("link", records=records, targets=targets, limit=limit)
            except ValueError as exc:
                st.error(f"Invalid records: {exc}")
        else:
            strings = [line.strip() for line in (raw or "").splitlines() if line.strip()]
            if strings:
                core.new_run("link", strings=strings, parser=parser, targets=targets, limit=limit)
            else:
                st.error("Paste at least one reference.")

run = core.current_run("link")
if run:
    records = run.get("records")
    if records is None:
        parsed = core.run_job(
            f"{run['id']}:parse",
            f"Parsing {len(run['strings'])} references ({run['parser']})",
            lambda: core.client().parse_references(run["strings"], parser=run["parser"]),
        )
        records = (parsed or {}).get("references") or []
    if records:
        linked, numbers = core.link_stage(f"{run['id']}:link", records, run["targets"], run["limit"])
        if linked:
            core.show_links(linked, numbers, "links", key="link-table")
            core.json_download(
                [{"reference": i.reference, "links": {t: vars(link) for t, link in i.links.items()}} for i in linked],
                "links",
                key="link-json",
            )

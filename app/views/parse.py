"""Single step: reference strings -> structured records."""

from __future__ import annotations

import json

import streamlit as st

import core

st.title("Parse")
st.caption("Turn raw reference strings into structured records (title, authors, year, …).")

with st.form("parse-form"):
    text = st.text_area(
        "References, one per line",
        height=240,
        key="parse-input",
        placeholder="Smith, J. (2020). Title. Journal, 1(2), 3–4. doi:10.1234/abc",
    )
    parser = st.selectbox("Parser", core.PARSERS)
    with st.expander("LLM options"):
        prompt_name = st.text_input("Prompt template", "prompts/reference_parsing.md")
        temperature = st.slider("Temperature", 0.0, 2.0, 0.0, 0.1)
    if st.form_submit_button("Parse", type="primary"):
        references = [line.strip() for line in text.splitlines() if line.strip()]
        if not references:
            st.error("Paste at least one reference.")
        else:
            core.new_run(
                "parse", references=references, parser=parser, prompt_name=prompt_name, temperature=temperature
            )

run = core.current_run("parse")
if run:
    result = core.run_job(
        f"{run['id']}:parse",
        f"Parsing {len(run['references'])} references ({run['parser']})",
        lambda: core.client().parse_references(
            run["references"], parser=run["parser"], prompt_name=run["prompt_name"], temperature=run["temperature"]
        ),
    )
    records = (result or {}).get("references") or []
    if records:
        if st.button("Link these records →"):
            core.handoff("views/link.py", "link-prefill", json.dumps(records, indent=1, ensure_ascii=False))
        frame = core.records_frame(records)
        if len(records) == len(run["references"]):
            frame.insert(1, "reference", run["references"])
        core.show_table(frame, "parsed-references", key="parse-table")
        core.json_download(records, "parsed-references", key="parse-json")

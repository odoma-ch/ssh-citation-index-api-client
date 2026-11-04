"""Single step: citation intent — is a work cited for its method, background or result?"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import core

st.title("Classify")
st.caption(
    "Classify why a work is cited: **method**, **background** or **result**. Add one "
    "citing sentence per row; the section title helps but may be left empty."
)

prefill = st.session_state.pop("classify-prefill", None)
if prefill is not None:
    st.session_state["classify-rows"] = pd.DataFrame(prefill).reindex(columns=["section", "citation"])
initial = st.session_state.setdefault(
    "classify-rows",
    pd.DataFrame(
        [
            {"section": "Methods", "citation": "We follow the annotation scheme of Smith et al. (2020)."},
            {"section": "", "citation": "Citation analysis has a long history (Garfield, 1955)."},
        ]
    ),
)

with st.form("classify-form"):
    rows = st.data_editor(
        initial,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "section": st.column_config.TextColumn("Section", width="small"),
            "citation": st.column_config.TextColumn("Citing sentence", width="large", required=True),
        },
    )
    mode = st.selectbox("Mode", list(core.CLASSIFY_MODES), format_func=core.CLASSIFY_MODES.get)
    if st.form_submit_button("Classify", type="primary"):
        citations = [
            {"section": str(r.get("section") or ""), "citation": str(r["citation"]).strip()}
            for r in rows.fillna("").to_dict("records")
            if str(r.get("citation") or "").strip()
        ]
        if citations:
            core.new_run("classify", citations=citations, mode=mode)
        else:
            st.error("Add at least one citing sentence.")

run = core.current_run("classify")
if run:
    intents, raw = core.classify_stage(f"{run['id']}:classify", run["citations"], run["mode"])
    if intents is not None and not intents.empty:
        core.show_intents(intents, "citation-intents", key="classify-table")
        core.json_download(raw, "citation-intents", key="classify-json", label="Download full result (JSON)")

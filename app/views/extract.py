"""Single step: extraction — PDF to text, Markdown to references, PDF to references."""

from __future__ import annotations

import json

import streamlit as st

import core

st.title("Extract")
pdf_text, md_refs, pdf_refs = st.tabs(
    ["PDF → text", "Markdown → references (LLM)", "PDF → references (GROBID)"]
)

with pdf_text:
    with st.form("extract-text"):
        upload = st.file_uploader("PDF", type="pdf", key="extract-text-pdf")
        left, right = st.columns(2)
        extractor = left.selectbox("Extractor", core.EXTRACTORS)
        markdown = right.toggle("Markdown output", value=True)
        if st.form_submit_button("Extract text", type="primary"):
            if upload is None:
                st.error("Upload a PDF first.")
            else:
                core.new_run("extract-text", pdf=core.upload_payload(upload), extractor=extractor, markdown=markdown)
    run = core.current_run("extract-text")
    if run:
        pdf = run["pdf"]
        result = core.run_job(
            f"{run['id']}:text",
            f"Extracting text from {pdf['name']} ({run['extractor']})",
            lambda: core.client().extract_text(
                core.as_file(pdf), extractor=run["extractor"], markdown=run["markdown"], filename=pdf["name"]
            ),
        )
        text = (result or {}).get("text") or ""
        if text:
            left, right = st.columns([1, 3])
            if left.button("Find references in this text", key="text-to-refs"):
                st.session_state["extract-md-input"] = text
                st.toast("Text copied to the “Markdown → references” tab.")
            right.download_button(
                "Download text", text, f"{core.file_stem(pdf['name'])}.{'md' if run['markdown'] else 'txt'}",
                on_click="ignore",
            )
            with st.container(height=500):
                if run["markdown"]:
                    st.markdown(text)
                else:
                    st.text(text)

with md_refs:
    with st.form("extract-md"):
        text = st.text_area(
            "Document text (Markdown)",
            height=240,
            key="extract-md-input",
            placeholder="Paste the document, including its bibliography…",
        )
        left, right = st.columns(2)
        method = left.selectbox("Method", core.LLM_METHODS, help="semantic splits long documents by meaning first.")
        temperature = right.slider("Temperature", 0.0, 2.0, 0.3, 0.1)
        if st.form_submit_button("Find references", type="primary"):
            if not text.strip():
                st.error("Paste some text first.")
            else:
                core.new_run("extract-md", text=text, method=method, temperature=temperature)
    run = core.current_run("extract-md")
    if run:
        result = core.run_job(
            f"{run['id']}:refs",
            f"Finding references ({run['method']})",
            lambda: core.client().extract_references(run["text"], method=run["method"], temperature=run["temperature"]),
        )
        references = (result or {}).get("references") or []
        if result is not None:
            st.caption(f"{len(references)} references found.")
        if references:
            if st.button("Parse these references →", key="md-to-parse"):
                core.handoff("views/parse.py", "parse-input", "\n".join(references))
            core.show_table(core.strings_frame(references), "references", key="md-refs")

with pdf_refs:
    with st.form("extract-pdf"):
        upload = st.file_uploader("PDF", type="pdf", key="extract-pdf-file")
        left, right = st.columns(2)
        include_parsed = left.toggle("Also parse references", value=True)
        include_context = right.toggle("Also return citing sentences", value=True)
        if st.form_submit_button("Extract references", type="primary"):
            if upload is None:
                st.error("Upload a PDF first.")
            else:
                core.new_run(
                    "extract-pdf",
                    pdf=core.upload_payload(upload),
                    include_parsed=include_parsed,
                    include_context=include_context,
                )
    run = core.current_run("extract-pdf")
    if run:
        pdf = run["pdf"]
        result = core.run_job(
            f"{run['id']}:refs",
            f"Extracting references from {pdf['name']} (GROBID)",
            lambda: core.client().extract_references_from_pdf(
                core.as_file(pdf),
                include_parsed=run["include_parsed"],
                include_context=run["include_context"],
                filename=pdf["name"],
            ),
        )
        if result:
            stem = core.file_stem(pdf["name"])
            references = result.get("references") or []
            parsed = result.get("parsed_references") or []
            citations = core.flatten_contexts(result.get("contexts"))
            st.caption(f"{len(references)} references, {len(citations)} citing sentences.")
            buttons = st.columns(3)
            if references and buttons[0].button("Parse references →", key="pdf-to-parse"):
                core.handoff("views/parse.py", "parse-input", "\n".join(references))
            if parsed and buttons[1].button("Link parsed records →", key="pdf-to-link"):
                core.handoff("views/link.py", "link-prefill", json.dumps(parsed, indent=1, ensure_ascii=False))
            if citations and buttons[2].button("Classify citing sentences →", key="pdf-to-classify"):
                core.handoff("views/classify.py", "classify-prefill", citations)
            frame = core.records_frame(parsed) if parsed else core.strings_frame(references)
            if parsed and len(parsed) == len(references):
                frame.insert(1, "reference", references)
            core.show_table(frame, f"{stem}-references", key="pdf-refs")
            if citations:
                st.markdown("**Citing sentences**")
                core.show_table(
                    core.pd.DataFrame(citations).reindex(columns=["reference", "marker", "section", "citation"]),
                    f"{stem}-citations",
                    key="pdf-citations",
                )
            core.json_download(result, f"{stem}-grobid", key="pdf-json")

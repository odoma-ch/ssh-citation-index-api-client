"""End-to-end: PDF or Markdown -> references -> parsed records -> links, intents, OpenCitations CSV."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import core
from citation_index_api_client import CitingDocument, to_opencitations

RUN = "pipeline"
TEXT_SUFFIXES = (".md", ".markdown", ".txt")
ROUTES = {
    "grobid": "GROBID — references, parsed records and citing sentences in one job",
    "llm": "LLM — text extraction, then LLM extraction and parsing (no classification)",
}

st.title("End-to-end pipeline")
st.caption(
    "Upload a paper (PDF, or Markdown text) to extract its references, parse them into "
    "structured records, link them to scholarly indexes, classify why each one is cited "
    "and export an OpenCitations deposit."
)

run = core.current_run(RUN)
# Collapse the form once a run exists, so its progress and results sit near the
# top instead of below a long form. The label change makes it a new expander.
form_box = st.expander(
    "New run — upload another paper" if run else "Upload a paper",
    expanded=run is None,
    icon=":material/upload_file:",
)
with form_box, st.form("pipeline-form"):
    upload = st.file_uploader(
        "Paper (PDF or Markdown)",
        type=["pdf", "md", "markdown", "txt"],
        help="Markdown skips text extraction and always takes the LLM route.",
    )
    route = st.radio("Extraction route", list(ROUTES), format_func=ROUTES.get)
    # ponytail: no nested expander here — older Streamlit raises on expander-in-expander.
    left, right = st.columns(2)
    extractor = left.selectbox(
        "Text extractor (LLM route)", ["pymupdf", "mineru"], help="MinerU is slower but better on complex layouts."
    )
    parser = right.selectbox("Parser (LLM route)", core.PARSERS)
    left, right = st.columns(2)
    with left:
        do_link = st.toggle("Link references", value=True)
        targets, limit = core.link_options(RUN)
    with right:
        do_classify = st.toggle(
            "Classify citation intent",
            value=True,
            help="Needs the citing sentences only the GROBID route returns.",
        )
        mode = st.selectbox("Classifier mode", list(core.CLASSIFY_MODES), format_func=core.CLASSIFY_MODES.get)
    if st.form_submit_button("Run pipeline", type="primary"):
        if upload is None:
            st.error("Upload a PDF first.")
        elif do_link and not targets:
            st.error("Pick at least one linking target, or turn linking off.")
        else:
            is_text = upload.name.lower().endswith(TEXT_SUFFIXES)
            route = "llm" if is_text else route
            core.new_run(
                RUN,
                pdf=core.upload_payload(upload),
                is_text=is_text,
                route=route,
                extractor=extractor,
                parser=parser,
                link=do_link,
                targets=targets,
                limit=limit,
                classify=do_classify and route == "grobid",
                mode=mode,
            )
            st.rerun()


def extract(run: dict[str, Any]) -> dict[str, Any]:
    """Run the extraction stages; returns whatever they produced, even partially."""
    rid, pdf, client = run["id"], run["pdf"], core.client()
    if run["route"] == "grobid":
        result = core.run_job(
            f"{rid}:grobid",
            "Extracting and parsing references (GROBID)",
            lambda: client.process_references_grobid(
                core.as_file(pdf), include_context=True, filename=pdf["name"]
            ),
        )
        result = result or {}
        return {
            "references": result.get("references") or [],
            "parsed": result.get("parsed_references") or [],
            "contexts": result.get("contexts") or [],
        }

    out: dict[str, Any] = {"references": [], "parsed": [], "contexts": []}
    if run.get("is_text"):
        out["text"] = pdf["data"].decode("utf-8", errors="replace")
    else:
        text = core.run_job(
            f"{rid}:text",
            f"Extracting text ({run['extractor']})",
            lambda: client.extract_text(core.as_file(pdf), extractor=run["extractor"], filename=pdf["name"]),
        )
        out["text"] = (text or {}).get("text") or ""
    if not out["text"].strip():
        return out
    found = core.run_job(
        f"{rid}:refs", "Finding references (LLM)", lambda: client.extract_references(out["text"])
    )
    out["references"] = (found or {}).get("references") or []
    if not out["references"]:
        return out
    parsed = core.run_job(
        f"{rid}:parse",
        f"Parsing {len(out['references'])} references ({run['parser']})",
        lambda: client.parse_references(out["references"], parser=run["parser"]),
    )
    out["parsed"] = (parsed or {}).get("references") or []
    return out


def references_table(out: dict[str, Any], intents: pd.DataFrame | None) -> pd.DataFrame:
    """Raw string, intents and parsed fields side by side, one row per reference."""
    frame = core.records_frame(out["parsed"]) if out["parsed"] else core.strings_frame(out["references"])
    if out["parsed"] and len(out["references"]) == len(out["parsed"]):
        frame.insert(1, "reference", out["references"])
    if intents is not None and not intents.empty:
        summary = core.intent_summary(intents)
        frame.insert(2 if "reference" in frame else 1, "intents", frame["#"].map(summary).fillna(""))
    return frame


def report(run: dict[str, Any], out: dict[str, Any], hits: int, lookups: int, citations: int) -> None:
    """Fill the banner at the top of the results with the run's outcome."""
    failures = core.run_failures(run["id"])
    if failures:
        names = "; ".join(job["label"] for job in failures)
        banner.error(
            f"**{len(failures)} step(s) failed:** {names}. Each failed step below shows "
            "its error and a retry button; results from the other steps are kept.",
            icon=":material/error:",
        )
        message, icon = "Pipeline finished with errors", ":material/error:"
    elif not out["references"]:
        banner.warning("**Finished, but no references were found in this PDF.**", icon=":material/warning:")
        message, icon = "Pipeline finished — no references found", ":material/warning:"
    else:
        parts = [f"{len(out['references'])} references", f"{len(out['parsed'])} parsed"]
        if lookups:
            parts.append(f"{hits}/{lookups} link matches")
        if citations:
            parts.append(f"{citations} citing sentences classified")
        banner.success(f"**Done** · {' · '.join(parts)}. Results are in the tabs below.", icon=":material/check_circle:")
        message, icon = "Pipeline finished", ":material/check_circle:"
    if not run.get("notified"):
        st.toast(message, icon=icon)
        run["notified"] = True


def citing_metadata(run_id: str, pdf: dict[str, Any]) -> dict[str, Any] | None:
    """The citing paper's own record from /extract/metadata, or None if none was found."""
    method = st.radio(
        "Citing paper metadata",
        ["grobid", "llm"],
        horizontal=True,
        key=f"oc-method-{run_id}",
        help="GROBID reads the PDF header (~3 s, reuses the references run). "
        "LLM reads the first pages (~20 s): better authors, fills journal and publisher.",
    )
    # Not under "{run_id}:", so a paper without metadata does not mark the run failed.
    result = core.run_job(
        f"{run_id}-metadata:{method}",
        f"Citing paper metadata ({method})",
        lambda: core.client().extract_metadata(core.as_file(pdf), method=method, filename=pdf["name"]),
    )
    return (result or {}).get("metadata") or None


def opencitations_export(
    run_id: str,
    parsed: list[Any],
    linked: list[Any],
    numbers: list[int],
    stem: str,
    pdf: dict[str, Any] | None = None,
) -> None:
    """META-CSV + CITS-CSV deposit for the citing paper (issue #54)."""
    st.caption(
        "One deposit file: META-CSV, the `===###===@@@===` line, then CITS-CSV "
        "([format](https://github.com/opencitations/crowdsourcing)). Linked references get "
        "DOI / OpenAlex / Wikidata / OMID ids; Matilda ids are not accepted, so with "
        "Matilda alone most rows carry `temp:` ids. Check it with oc_validator before depositing."
    )
    metadata = citing_metadata(run_id, pdf) if pdf else None
    label = "Enter the citing paper by hand" + (" (overrides the metadata)" if metadata else "")
    with st.expander(label, expanded=metadata is None):
        left, right = st.columns(2)
        doi = left.text_input("Citing paper id", placeholder="doi:10.1234/abc", key=f"oc-id-{run_id}")
        title = right.text_input("Citing paper title (if no id)", key=f"oc-title-{run_id}")
        left, right = st.columns(2)
        date = left.text_input("Publication date (if no id)", placeholder="YYYY or YYYY-MM-DD", key=f"oc-date-{run_id}")
        authors = right.text_input("Authors (if no id)", placeholder="Surname, Given; Surname, Given", key=f"oc-authors-{run_id}")
    typed = any(v.strip() for v in (doi, title, date, authors))
    citing: Any = metadata if metadata and not typed else CitingDocument(
        ids=doi.strip(),
        title=title,
        pub_date=date.strip(),
        authors=[a for a in authors.split(";") if a.strip()],
    )
    # Linking covers only the linkable records, so map results back by record number.
    links: list[Any] = [None] * len(parsed)
    for number, item in zip(numbers, linked):
        links[number - 1] = item
    try:
        deposit = to_opencitations(parsed, links, citing=citing)
    except ValueError as exc:
        st.info(f"{exc}. Fill in the citing paper above to build the deposit.")
        return
    st.caption("Citing paper row (first META row):")
    st.dataframe(pd.DataFrame(deposit.meta[:1]), hide_index=True, width="stretch")
    cols = st.columns(3)
    cols[0].metric("Works (META rows)", len(deposit.meta) - 1)
    cols[1].metric("Merged repeats", deposit.merged)
    cols[2].metric("Dropped", len(deposit.dropped))
    st.download_button(
        "Download OpenCitations deposit (CSV)",
        deposit.text.encode("utf-8"),
        file_name=f"{stem}-opencitations.csv",
        mime="text/csv",
        key="oc-csv",
        on_click="ignore",
        type="primary",
    )
    if deposit.dropped:
        with st.expander(f"{len(deposit.dropped)} references left out"):
            st.dataframe(
                pd.DataFrame([{"#": d.record, "reason": d.reason} for d in deposit.dropped]),
                hide_index=True,
                width="stretch",
            )
    st.dataframe(pd.DataFrame(deposit.meta), hide_index=True, width="stretch")


if run:
    st.subheader(run["pdf"]["name"])
    banner = st.empty()
    banner.info(
        "**Running…** each step reports below. You can switch pages — coming back "
        "resumes the same jobs.",
        icon=":material/hourglass_top:",
    )
    out = extract(run)
    linked, numbers = [], []
    if run["link"] and out["parsed"]:
        linked, numbers = core.link_stage(f"{run['id']}:link", out["parsed"], run["targets"], run["limit"])
    citations = core.flatten_contexts(out["contexts"])
    intents, raw_intents = None, None
    if run["classify"]:
        if citations:
            intents, raw_intents = core.classify_stage(f"{run['id']}:classify", citations, run["mode"])
        elif out["references"]:
            st.info(
                "GROBID found no in-text citing sentences (common in footnote-style "
                "papers), so there is nothing to classify."
            )

    hits, lookups = core.link_stats(linked)
    report(run, out, hits, lookups, 0 if intents is None else len(intents))
    cols = st.columns(4)
    cols[0].metric("References", len(out["references"]))
    cols[1].metric("Parsed records", len(out["parsed"]))
    cols[2].metric("Link matches", f"{hits}/{lookups}" if lookups else "—")
    cols[3].metric("Citing sentences", len(citations) if run["route"] == "grobid" else "—")

    stem = core.file_stem(run["pdf"]["name"])
    tabs = st.tabs(
        ["References", "Links", "Citation intents", "OpenCitations", "Document text", "Everything (JSON)"]
    )
    with tabs[0]:
        if out["references"]:
            core.show_table(references_table(out, intents), f"{stem}-references", key="pipe-refs")
        else:
            st.caption("No references yet.")
    with tabs[1]:
        if linked:
            core.show_links(linked, numbers, f"{stem}-links", key="pipe-links")
        else:
            st.caption("Linking was off or did not run." if not run["link"] else "No links yet.")
    with tabs[2]:
        if intents is not None and not intents.empty:
            core.show_intents(intents, f"{stem}-intents", key="pipe-intents")
        elif run["route"] == "llm":
            st.caption("Classification needs citing sentences, which only the GROBID route returns.")
        else:
            st.caption("No citation intents.")
    with tabs[3]:
        if out["parsed"]:
            opencitations_export(
                run["id"], out["parsed"], linked, numbers, stem,
                pdf=None if run.get("is_text") else run["pdf"],
            )
        else:
            st.caption("The export needs parsed references.")
    with tabs[4]:
        if out.get("text"):
            st.download_button("Download Markdown", out["text"], f"{stem}.md", on_click="ignore")
            with st.container(height=500):
                st.markdown(out["text"])
        else:
            st.caption("The GROBID route works on the PDF directly, without a text step.")
    with tabs[5]:
        bundle = {
            **{k: v for k, v in out.items() if k != "text"},
            "links": [
                {"record": n, "reference": item.reference, "links": {t: vars(link) for t, link in item.links.items()}}
                for n, item in zip(numbers, linked)
            ],
            "classification": raw_intents,
        }
        core.json_download(bundle, f"{stem}-pipeline", key="pipe-json", label="Download everything (JSON)")
        st.json(bundle, expanded=1)

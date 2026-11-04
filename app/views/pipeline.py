"""End-to-end: PDF -> references -> parsed records -> links + citation intents."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import core

RUN = "pipeline"
ROUTES = {
    "grobid": "GROBID — references, parsed records and citing sentences in one job",
    "llm": "LLM — text extraction, then LLM extraction and parsing (no classification)",
}

st.title("End-to-end pipeline")
st.caption(
    "Upload a paper to extract its references, parse them into structured records, "
    "link them to scholarly indexes and classify why each one is cited."
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
    upload = st.file_uploader("Paper (PDF)", type="pdf")
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
            core.new_run(
                RUN,
                pdf=core.upload_payload(upload),
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
    tabs = st.tabs(["References", "Links", "Citation intents", "Document text", "Everything (JSON)"])
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
        if out.get("text"):
            st.download_button("Download Markdown", out["text"], f"{stem}.md", on_click="ignore")
            with st.container(height=500):
                st.markdown(out["text"])
        else:
            st.caption("The GROBID route works on the PDF directly, without a text step.")
    with tabs[4]:
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

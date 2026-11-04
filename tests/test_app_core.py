"""Pure helpers behind the Streamlit app (no server, no API calls)."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import core  # noqa: E402
from citation_index_api_client import LinkedReference  # noqa: E402

# Shape returned by /process/references/grobid: one list of contexts per reference.
CONTEXTS = [
    [
        {"section": "Intro", "citation": "As shown (Cebe, 2012).", "marker": "(Cebe, 2012)"},
        {"section": "Intro", "citation": "   "},
    ],
    [],
    [{"section": "", "citation": "Background (Doe, 2000).", "marker": "(Doe, 2000)"}],
]
# Shape returned by /classify/citations, in the order submitted.
RESULTS = [
    {"FINAL PREDICTION": "usesMethodIn (METHOD)", "MET ENSEMBLE CONFIDENCE": 0.91234},
    {"FINAL PREDICTION": "obtainsBackgroundFrom (BACKGROUND)", "BKG ENSEMBLE CONFIDENCE": 0.99},
]


def test_flatten_contexts_keeps_reference_numbers_and_drops_empty():
    flat = core.flatten_contexts(CONTEXTS)
    assert [c["reference"] for c in flat] == [1, 3]
    assert core.classifier_input(flat) == [
        {"section": "Intro", "citation": "As shown (Cebe, 2012)."},
        {"section": "", "citation": "Background (Doe, 2000)."},
    ]
    assert core.flatten_contexts(None) == []


def test_intents_join_back_to_references():
    flat = core.flatten_contexts(CONTEXTS)
    frame = core.intents_frame(flat, RESULTS)
    assert frame["#"].tolist() == [1, 3]
    assert frame["intent"].tolist() == ["method", "background"]
    assert frame.loc[0, "method"] == 0.912
    assert core.intent_summary(frame) == {1: "method", 3: "background"}


def test_links_frame_uses_record_numbers():
    linked = [
        LinkedReference.from_dict(
            {"reference": {"full_title": "T"}, "links": {"openalex": {"id": "https://openalex.org/W1", "doi": None}}}
        )
    ]
    frame = core.links_frame(linked, [4])
    assert frame.loc[0, "#"] == 4
    assert frame.loc[0, "openalex"] == "https://openalex.org/W1"
    assert core.link_stats(linked) == (1, 1)


def test_records_frame_orders_known_fields_and_flattens_people():
    frame = core.records_frame(
        [{"raw": {}, "publication_year": 2020, "authors": [{"first_name": "J", "surname": "Cebe"}], "full_title": "T"}]
    )
    assert list(frame.columns) == ["#", "full_title", "authors", "publication_year"]
    assert frame.loc[0, "authors"] == "J Cebe"


def test_short_error_strips_html_pages_and_truncates():
    html = "GROBID service error (HTTP 503): <!DOCTYPE html>\n<html><body>503</body></html>"
    assert core.short_error(html) == "GROBID service error (HTTP 503) (the service returned an HTML error page)"
    assert core.short_error("plain < comparison") == "plain < comparison"
    assert core.short_error("x" * 500, limit=10) == "x" * 10 + "…"


def test_result_summary_counts_lists_and_nested_contexts():
    grobid = {"references": ["a", "b"], "parsed_references": [{}, {}], "contexts": [[{}, {}], []], "count": 2}
    assert core.result_summary(grobid) == "2 references · 2 parsed references · 2 contexts"
    assert core.result_summary({"text": "x" * 100}) == "100 characters of text"
    assert core.result_summary({"count": 0}) == "a result"

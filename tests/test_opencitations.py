import csv
import io
import warnings

import pytest

from citation_index_api_client import CitingDocument, LinkedReference, to_opencitations
from citation_index_api_client.opencitations import SEPARATOR, _page

CITING = CitingDocument(ids="doi:10.1234/citing")

ARTICLE = {
    "full_title": "Citation networks in the humanities",
    "authors": [{"first_name": "Jan", "surname": "Berg", "name_link": "van der"}],
    "journal_title": "Journal of Documentation",
    "publication_year": 2019,
    "volume": "75",
    "issue": "2",
    "pages": "27–82",
    "ref_type": "journal-article",
    "identifiers": [
        {"scheme": "doi", "value": "https://doi.org/10.1108/JD-01-2019", "normalized": None},
        {"scheme": "issn", "value": "00220418", "normalized": None},
    ],
}
CHAPTER = {
    "full_title": "Le notaire et la ville",
    "authors": [{"first_name": "Marie", "middle_name": "C.", "surname": "Dupont"}],
    "editors": [{"first_name": "Paul", "surname": "Martin"}],
    "container_title": "Histoire urbaine",
    "publication_year": 1989,
    "pages": "p. 27",
    "ref_type": None,
}
GROBID_CHAPTER = {
    "full_title": "Merchants of Venice",
    "authors": [{"first_name": "Luca", "surname": "Rossi"}],
    "publication_year": 2001,
    "ref_type": None,
    "raw": {"tei": {"analytic_title": "Merchants of Venice", "monographic_title": "Trade [and] Empire"}},
}
BOOK_VOL = {
    "full_title": "Storia d'Italia",
    "authors": ["Croce, Benedetto"],
    "publisher": "Laterza",
    "publication_year": 1928,
    "volume": "vol. 2",
    "pages": "350 pp.",
    "identifiers": [{"scheme": "isbn", "value": "978-0-12-345678-6", "normalized": "9780123456786"}],
}
IBID = {"full_title": "Ibid.", "authors": None, "publication_year": None, "pages": "45"}


def rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def by_title(deposit):
    return {row["title"]: row for row in deposit.meta}


def test_journal_article_with_doi_and_issn():
    deposit = to_opencitations([ARTICLE], citing=CITING)
    row = by_title(deposit)[ARTICLE["full_title"]]
    assert row["id"] == "doi:10.1108/jd-01-2019"
    assert row["venue"] == "Journal of Documentation [issn:0022-0418]"
    assert row["author"] == "van der Berg, Jan"
    assert (row["volume"], row["issue"], row["page"]) == ("75", "2", "27-82")
    assert row["type"] == "journal article"
    assert deposit.cits == ({"citing_id": "doi:10.1234/citing", "cited_id": "doi:10.1108/jd-01-2019"},)


def test_chapter_without_ids_gets_temp_and_container_venue():
    deposit = to_opencitations([CHAPTER, GROBID_CHAPTER], citing=CITING)
    rows_ = by_title(deposit)
    chapter = rows_[CHAPTER["full_title"]]
    assert chapter["id"] == "temp:1"
    assert chapter["type"] == "book chapter"
    assert chapter["venue"] == "Histoire urbaine"
    assert chapter["author"] == "Dupont, Marie C."
    assert chapter["editor"] == "Martin, Paul"
    assert chapter["page"] == "27-27"
    grobid = rows_[GROBID_CHAPTER["full_title"]]
    assert (grobid["id"], grobid["venue"], grobid["type"]) == ("temp:2", "Trade and Empire", "book chapter")


def test_book_volume_without_venue_is_dropped_and_isbn_kept():
    row = by_title(to_opencitations([BOOK_VOL], citing=CITING))[BOOK_VOL["full_title"]]
    assert row["id"] == "isbn:9780123456786"
    assert row["type"] == "book"
    assert (row["venue"], row["volume"], row["page"]) == ("", "", "")
    assert row["author"] == "Croce, Benedetto"


@pytest.mark.parametrize(
    "ref_type, extra, expected",
    [
        ("article", {"journal_title": "J"}, "journal article"),
        ("dissertation", {}, "dissertation"),
        ("paper_conference", {}, "proceedings article"),
        ("book", {"container_title": "The Book"}, "book chapter"),  # LLM types chapters as books
        ("book", {}, "book"),
        ("poem", {}, "other"),
        (None, {"publisher": "OUP"}, "book"),
        (None, {}, ""),
    ],
)
def test_type_mapping(ref_type, extra, expected):
    record = {"full_title": "T", "authors": ["Doe, J"], "publication_year": 2000, "ref_type": ref_type, **extra}
    deposit = to_opencitations([record], citing=CITING)
    assert deposit.meta[1]["type"] == expected


def test_bad_omid_is_dropped():
    linked = {"links": {"opencitations": {"id": "br/0612345", "doi": None}}}
    deposit = to_opencitations([ARTICLE], [linked], citing=CITING)
    assert "omid:" not in deposit.meta[1]["id"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("27", "27-27"), ("27–82", "27-82"), ("pp. 27-82", "27-82"), ("27 — 82", "27-82"),
        ("350 pp.", ""), ("027-030", "27-30"), ("xi-xx", "xi-xx"), ("passim", ""), (None, ""),
        ("12, 15", ""),
    ],
)
def test_page(raw, expected):
    assert _page(raw) == expected


def test_repeated_footnote_is_merged_and_ibid_dropped():
    linked = LinkedReference.from_dict(
        {"reference": ARTICLE, "links": {
            "openalex": {"id": "https://openalex.org/W42", "doi": "10.1108/jd-01-2019"},
            "opencitations": {"id": "br/06101234", "doi": None},
            "matilda": {"id": "matilda-1", "doi": None},
            "wikidata": {"id": None, "doi": None, "error": "timeout"},
        }}
    )
    repeat = {**ARTICLE, "identifiers": [], "pages": "30"}
    deposit = to_opencitations([ARTICLE, IBID, repeat, CHAPTER, dict(CHAPTER)], [linked, None], citing=CITING)
    assert len(deposit.meta) == 3  # citing + article + chapter
    assert deposit.merged == 2
    assert [(d.record, d.reason.split(",")[0]) for d in deposit.dropped] == [(2, "short form or no title")]
    article = by_title(deposit)[ARTICLE["full_title"]]
    assert article["id"] == "doi:10.1108/jd-01-2019 openalex:W42 omid:br/06101234"
    assert len(deposit.cits) == len({(c["citing_id"], c["cited_id"]) for c in deposit.cits}) == 2


def test_temp_row_missing_mandatory_fields_is_dropped():
    no_year = {"full_title": "Undated pamphlet", "authors": ["Anon"]}
    deposit = to_opencitations([no_year], citing=CITING)
    assert len(deposit.meta) == 1
    assert "pub_date" in deposit.dropped[0].reason


def test_citing_document_without_doi():
    citing = CitingDocument(title="Notaries in early modern Lyon", pub_date="2021-05",
                            authors=["Rossi, Anna"])
    deposit = to_opencitations([CHAPTER], citing=citing)
    assert deposit.meta[0]["id"] == "temp:0"
    assert deposit.cits[0]["citing_id"] == "temp:0"
    with pytest.raises(ValueError):
        to_opencitations([CHAPTER], citing=CitingDocument(title="Only a title"))


# /extract/metadata result for the citing PDF (method=llm): a parsed-reference record.
CITING_METADATA = {
    "full_title": "The Story of Journalist Organizations in Czechoslovakia",
    "journal_title": "Media and Communication",
    "authors": [{"first_name": "Markéta", "surname": "Ševčíková"}],
    "publication_year": 2017,
    "volume": "5", "issue": "3", "pages": "95–102",
    "identifiers": [
        {"scheme": "doi", "value": "10.17645/mac.v5i3.1042", "normalized": "10.17645/mac.v5i3.1042"},
        {"scheme": "issn", "value": "2183-2439", "normalized": "21832439"},
    ],
}


def test_citing_document_from_metadata():
    deposit = to_opencitations([ARTICLE], citing=CITING_METADATA)
    row = deposit.meta[0]
    assert row["id"] == "doi:10.17645/mac.v5i3.1042"
    assert row["venue"] == "Media and Communication [issn:2183-2439]"
    assert (row["type"], row["page"], row["author"]) == ("journal article", "95-102", "Ševčíková, Markéta")
    assert deposit.cits[0]["citing_id"] == row["id"]


def test_citing_metadata_without_id():
    no_id = {**CITING_METADATA, "identifiers": []}
    assert to_opencitations([ARTICLE], citing=no_id).meta[0]["id"] == "temp:0"
    with pytest.raises(ValueError, match="pub_date"):
        to_opencitations([ARTICLE], citing={**no_id, "publication_year": None})


def test_deposit_text_layout():
    text = to_opencitations([ARTICLE], citing=CITING).text
    meta, cits = text.split(f"{SEPARATOR}\n")
    assert meta.startswith('"id","title","author","pub_date","venue"')
    assert rows(cits)[0]["cited_id"] == "doi:10.1108/jd-01-2019"


def test_oc_validator_accepts_deposit(tmp_path, monkeypatch):
    main = pytest.importorskip("oc_validator.main")
    monkeypatch.chdir(tmp_path)  # the validator writes a storage/ cache into the cwd
    linked = {"links": {
        "openalex": {"id": "https://openalex.org/W42", "doi": "10.1108/jd-01-2019"},
        "wikidata": {"id": "Q42", "doi": None},
        "opencitations": {"id": "https://w3id.org/oc/meta/br/06101234", "doi": None},
        "matilda": {"id": "matilda-1", "doi": None},
    }}
    linked_chapter = {"links": {"openalex": {"id": "https://openalex.org/W7", "doi": None}}}
    repeat = {**ARTICLE, "identifiers": [], "pages": "30"}
    deposits = [
        to_opencitations(
            [ARTICLE, IBID, CHAPTER, GROBID_CHAPTER, BOOK_VOL, repeat, dict(CHAPTER)],
            [linked, None, linked_chapter],
            citing=CITING,
        ),
        to_opencitations([CHAPTER, BOOK_VOL], citing=CitingDocument(
            title="Notaries in early modern Lyon", pub_date="2021", authors=["Rossi, Anna"])),
        to_opencitations([ARTICLE, CHAPTER], citing={**CITING_METADATA, "identifiers": []}),
    ]
    assert deposits[0].merged == 2
    assert "omid:br/06101234" in deposits[0].text and "wikidata:Q42" in deposits[0].text
    for n, deposit in enumerate(deposits):
        meta, cits = tmp_path / f"meta{n}.csv", tmp_path / f"cits{n}.csv"
        # oc_validator's ClosureValidator takes META and CITS as two files.
        meta_text, cits_text = deposit.text.split(f"{SEPARATOR}\n")
        meta.write_text(meta_text, encoding="utf-8")
        cits.write_text(cits_text, encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            validator = main.ClosureValidator(
                str(meta), str(tmp_path / f"meta_out{n}"), str(cits), str(tmp_path / f"cits_out{n}"),
                meta_kwargs={"verify_id_existence": False, "use_meta_endpoint": False},
                cits_kwargs={"verify_id_existence": False, "use_meta_endpoint": False},
            )
            valid = validator.validate()
        reports = "\n".join(p.read_text() for p in tmp_path.glob(f"*_out{n}/*.txt"))
        assert valid == (True, True), reports

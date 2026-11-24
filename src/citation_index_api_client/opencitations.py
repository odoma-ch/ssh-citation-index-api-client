"""OpenCitations deposit (META-CSV + CITS-CSV) from parsed references and links.

Format: https://github.com/opencitations/crowdsourcing. The rules below follow
oc_validator (https://github.com/opencitations/oc_validator), which is what
accepts or rejects a deposit. See issue odoma-ch/ssh-citation-index#54.

Example::

    deposit = to_opencitations(
        parsed_references,
        links,  # aligned with parsed_references, None where not linked
        citing=CitingDocument(ids="doi:10.1234/paper"),
    )
    Path("deposit.csv").write_text(deposit.text)
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Union

from citation_index_api_client.models import LinkedReference

META_FIELDS = (
    "id", "title", "author", "pub_date", "venue", "volume",
    "issue", "page", "type", "publisher", "editor",
)
CITS_FIELDS = ("citing_id", "cited_id")
SEPARATOR = "===###===@@@==="

# API ref_type -> OpenCitations type. GROBID sets the first four; the LLM parser
# writes CSL-style names ("article", "chapter", "dissertation", ...).
TYPE_MAP = {
    "journal-article": "journal article",
    "article": "journal article",
    "article-journal": "journal article",
    "book-chapter": "book chapter",
    "chapter": "book chapter",
    "book-section": "book chapter",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "proceedings-paper": "proceedings article",
    "paper-conference": "proceedings article",
    "conference-paper": "proceedings article",
    "thesis": "dissertation",
    "dissertation": "dissertation",
    "report": "report",
    "preprint": "preprint",
    "posted-content": "preprint",
}
_COMMON = {"doi", "pmid", "wikidata", "url", "openalex", "temp", "local", "omid"}
# Accepted id schemes per type, from oc_validator's id_type_alignment.json
# (only the types this module emits). An empty type is not checked.
ID_SCHEMES_BY_TYPE = {
    "journal article": _COMMON | {"pmcid", "arxiv"},
    "book": _COMMON | {"isbn", "arxiv"},
    "book chapter": _COMMON | {"arxiv"},
    "proceedings article": _COMMON | {"pmcid", "arxiv"},
    "dissertation": _COMMON | {"isbn", "pmcid", "arxiv"},
    "report": _COMMON | {"isbn", "pmcid", "arxiv"},
    "preprint": _COMMON | {"pmcid", "arxiv"},
    "other": _COMMON | {"wikipedia", "arxiv"},
}
ACCEPTED_SCHEMES = {
    "doi", "issn", "isbn", "pmid", "pmcid", "url", "wikidata",
    "wikipedia", "openalex", "omid", "jid", "arxiv",
}
# Types whose temp-only rows need title + date + author/editor (oc_validator).
_NEEDS_DATE_AND_AGENT = {
    "", "book", "dissertation", "journal article", "other", "proceedings article", "report",
}
_NEEDS_VENUE = {"book chapter"}

_DATE = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[012])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$")
_PAGE_PART = r"(?:[1-9]\d*|[IiVvXxLlCcDdMm]+|[A-Za-z]?[1-9]\d*|[1-9]\d*[A-Za-z]?)"
_PAGE = re.compile(rf"^{_PAGE_PART}-{_PAGE_PART}$")
_SHORT_FORM = re.compile(
    r"^(?:ibid(?:em)?|id(?:em)?|op\.?\s*cit|loc\.?\s*cit|cit|art\.?\s*cit)\.?$", re.IGNORECASE
)

Links = Union[LinkedReference, Mapping[str, Any], None]


@dataclass(frozen=True)
class CitingDocument:
    """The document whose references are deposited, typed in by hand.

    ``to_opencitations`` also takes the ``metadata`` of ``/extract/metadata``
    as ``citing`` instead.

    ``ids`` is space-separated ``scheme:value`` (a bare DOI is accepted). Without
    an id, ``title``, ``pub_date`` and ``authors`` are required.
    ``authors`` are ``"Surname, Given names"`` strings.
    """

    ids: str = ""
    title: str = ""
    pub_date: str = ""
    authors: Sequence[str] = ()
    type: str = ""
    venue: str = ""


@dataclass(frozen=True)
class Dropped:
    """A reference left out of the deposit; ``record`` is its 1-based position."""

    record: int
    reason: str


@dataclass(frozen=True)
class OpenCitationsDeposit:
    meta: tuple[dict[str, str], ...]
    cits: tuple[dict[str, str], ...]
    dropped: tuple[Dropped, ...] = ()
    merged: int = 0  # references folded into another reference's row

    @property
    def text(self) -> str:
        """The deposit: META-CSV, the separator line, then CITS-CSV, in one file."""
        meta = _write_csv(META_FIELDS, self.meta)
        cits = _write_csv(CITS_FIELDS, self.cits)
        return f"{meta}{SEPARATOR}\n{cits}"


@dataclass(frozen=True)
class _Work:
    records: tuple[int, ...]
    ids: tuple[str, ...] = ()  # real ids, including container ids still to sort out
    title: str = ""
    authors: tuple[str, ...] = ()
    editors: tuple[str, ...] = ()
    pub_date: str = ""
    venue: str = ""
    volume: str = ""
    issue: str = ""
    page: str = ""
    type: str = ""
    publisher: str = ""
    key: str = ""  # title + first surname + year, for merging repeated footnotes


def to_opencitations(
    records: Sequence[Mapping[str, Any] | None],
    links: Sequence[Links] | None = None,
    *,
    citing: CitingDocument | Mapping[str, Any],
) -> OpenCitationsDeposit:
    """Build an OpenCitations deposit from parsed references.

    ``links`` is index-aligned with ``records`` (``/link/references`` results,
    ``None`` where a record was not linked). Repeated references are merged,
    short forms (ibid., op. cit.) and rows oc_validator would reject are
    dropped and reported in ``dropped``. ``citing`` is a :class:`CitingDocument`
    or the ``metadata`` record of ``/extract/metadata``, mapped like a reference.
    """
    citing_row = _citing_row(citing)
    citing_ids = set(citing_row["id"].split())
    links = list(links or [])
    works, dropped = [], []
    for number, record in enumerate(records, start=1):
        if not record:
            continue
        link = links[number - 1] if number <= len(links) else None
        work = _work(number, record, _link_ids(link))
        if not work.title and not work.ids:
            dropped.append(Dropped(number, "short form or no title, and no identifier"))
            continue
        works.append(work)

    groups = _merge(works)
    meta, cits = [citing_row], []
    for work in groups:
        row, reason = _meta_row(work)
        if reason:
            dropped.extend(Dropped(n, reason) for n in work.records)
            continue
        if citing_ids & set(row["id"].split()):
            dropped.extend(Dropped(n, "same identifier as the citing document") for n in work.records)
            continue
        meta.append(row)
        cits.append({"citing_id": citing_row["id"], "cited_id": row["id"]})
    merged = sum(len(work.records) - 1 for work in groups)
    return OpenCitationsDeposit(
        tuple(meta), tuple(cits), tuple(sorted(dropped, key=lambda d: d.record)), merged
    )


# --- one reference ------------------------------------------------------------


def _work(number: int, record: Mapping[str, Any], link_ids: list[str]) -> _Work:
    title = _text(record.get("full_title"))
    if _SHORT_FORM.match(title):
        title = ""
    authors = _people(record.get("authors"))
    pub_date = _pub_date(record)
    container = _text(record.get("container_title")) or _grobid_container(record)
    journal = _text(record.get("journal_title"))
    ids = _unique([*_record_ids(record.get("identifiers")), *link_ids])
    surname = authors[0].split(",")[0].lower() if authors else ""
    norm_title = re.sub(r"\W+", "", title.lower())
    return _Work(
        records=(number,),
        ids=tuple(ids),
        title=title,
        authors=tuple(authors),
        editors=tuple(_people(record.get("editors"))),
        pub_date=pub_date,
        venue=journal or container,
        volume=_text(record.get("volume")),
        issue=_text(record.get("issue")),
        page=_page(record.get("pages")),
        type=_type(record, journal, container),
        publisher=_text(record.get("publisher")).replace("[", "").replace("]", ""),
        key=f"{norm_title}|{surname}|{pub_date[:4]}" if norm_title and surname and pub_date else "",
    )


def _type(record: Mapping[str, Any], journal: str, container: str) -> str:
    ref_type = re.sub(r"[\s_]+", "-", _text(record.get("ref_type")).lower())
    if ref_type:
        mapped = TYPE_MAP.get(ref_type, "other")
        # A "book" inside another book is a chapter (the LLM parser types them so).
        return "book chapter" if mapped == "book" and container and not journal else mapped
    if journal:
        return "journal article"
    if container:
        return "book chapter"
    if record.get("publisher") or record.get("publication_place"):
        return "book"
    return ""


def _grobid_container(record: Mapping[str, Any]) -> str:
    # Before container_title, GROBID kept a chapter's book title here.
    tei = (record.get("raw") or {}).get("tei") or {}
    if isinstance(tei, Mapping) and tei.get("analytic_title"):
        return _text(tei.get("monographic_title"))
    return ""


def _pub_date(record: Mapping[str, Any]) -> str:
    date = _text(record.get("publication_date"))
    if _DATE.match(date):
        return date
    year = record.get("publication_year")
    if isinstance(year, int) and 1000 <= year <= 9999:
        return str(year)
    # A range like "1989-1990": the first year.
    match = re.search(r"\b(\d{4})\b", f"{date} {_text(record.get('publication_date_raw'))}")
    return match.group(1) if match else ""


def _page(value: Any) -> str:
    text = _text(value)
    if not text or re.fullmatch(r"\d+\s*pp?\.?", text, re.IGNORECASE):  # "350 pp." is a total
        return ""
    text = re.sub(r"^pp?\.\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[‐‑‒–—―-]+\s*", "-", text)
    if re.fullmatch(r"\w+", text):
        text = f"{text}-{text}"
    text = "-".join(part.lstrip("0") or "0" for part in text.split("-"))
    return text if _PAGE.match(text) else ""


def _people(value: Any) -> list[str]:
    names = []
    for item in value or []:
        if isinstance(item, Mapping):
            if item.get("name"):  # Organization
                name = _name_part(item["name"])
            else:
                surname = _name_part(" ".join(_text(item.get(k)) for k in ("name_link", "surname")))
                given = _name_part(" ".join(_text(item.get(k)) for k in ("first_name", "middle_name")))
                name = f"{surname}, {given}" if surname and given else (f"{surname}," if surname else given)
        else:
            surname, _, given = _text(item).partition(",")
            name = f"{_name_part(surname)}, {_name_part(given)}".strip(" ,") if given else _name_part(surname)
        if name and name not in names:
            names.append(name)
    return names


def _name_part(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[,;\[\]]", " ", _text(value))).strip()


# --- identifiers --------------------------------------------------------------


def _record_ids(identifiers: Any) -> list[str]:
    ids = []
    for item in identifiers or []:
        if not isinstance(item, Mapping):
            continue
        scheme = _text(item.get("scheme")).lower()
        normalized = _normalize_id(scheme, item.get("normalized") or item.get("value"))
        if normalized:
            ids.append(normalized)
    return ids


def _link_ids(link: Links) -> list[str]:
    if link is None:
        return []
    if not isinstance(link, LinkedReference):
        link = LinkedReference.from_dict({"reference": {}, **link})
    ids = []
    for target, found in link.links.items():
        if found.doi:
            ids.append(_normalize_id("doi", found.doi))
        if not found.id:
            continue
        if target == "openalex":
            ids.append(_normalize_id("openalex", found.id))
        elif target == "wikidata":
            ids.append(_normalize_id("wikidata", found.id))
        elif target == "opencitations":
            ids.append(_normalize_id("omid", found.id))
        # matilda ids are not an accepted scheme
    return [i for i in ids if i]


def _normalize_id(scheme: str, value: Any) -> str:
    """``scheme:value`` in the syntax oc_validator accepts, or ``""``."""
    text = _text(value)
    if scheme not in ACCEPTED_SCHEMES or not text:
        return ""
    if scheme == "doi":
        text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", text, flags=re.IGNORECASE).lower()
        ok = text.startswith("10.")
    elif scheme == "isbn":
        text = re.sub(r"[^0-9X]", "", text.upper())
        ok = len(text) in (10, 13)
    elif scheme == "issn":
        text = re.sub(r"[^0-9X]", "", text.upper())
        ok = len(text) == 8
        text = f"{text[:4]}-{text[4:]}"
    elif scheme == "url":
        text = re.sub(r"^https?://", "", text).rstrip("/")
        ok = "." in text
    elif scheme == "openalex":
        text = text.rstrip("/").rsplit("/", 1)[-1]
        ok = bool(re.fullmatch(r"W\d+", text))
    elif scheme == "wikidata":
        text = text.rstrip("/").rsplit("/", 1)[-1]
        ok = bool(re.fullmatch(r"Q\d+", text))
    elif scheme == "omid":
        # oc_validator's OMID pattern (br/ entities only).
        match = re.search(r"br/06[1-9]*0[1-9]\d*$", text)
        text = match.group(0) if match else ""
        ok = bool(text)
    elif scheme == "pmcid":
        text = text.upper() if text.upper().startswith("PMC") else f"PMC{text}"
        ok = True
    else:
        ok = True
    return f"{scheme}:{text}" if ok and not re.search(r"\s", text) else ""


# --- merging and rows ---------------------------------------------------------


def _merge(works: list[_Work]) -> list[_Work]:
    """One work per group sharing a real id or a title/surname/year key."""
    parent = list(range(len(works)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[str, int] = {}
    for i, work in enumerate(works):
        for key in (*work.ids, *([f"key:{work.key}"] if work.key else [])):
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    groups: dict[int, _Work] = {}
    for i, work in enumerate(works):
        root = find(i)
        groups[root] = _combine(groups[root], work) if root in groups else work
    return list(groups.values())


def _combine(first: _Work, other: _Work) -> _Work:
    """Keep the first work's values; fill its gaps from the other."""
    filled = {
        name: getattr(other, name)
        for name in ("title", "authors", "editors", "pub_date", "venue", "volume",
                     "issue", "page", "type", "publisher")
        if not getattr(first, name)
    }
    return replace(
        first,
        records=first.records + other.records,
        ids=tuple(_unique([*first.ids, *other.ids])),
        **filled,
    )


def _meta_row(work: _Work) -> tuple[dict[str, str], str]:
    """The META row, or the reason it would not validate."""
    allowed = ID_SCHEMES_BY_TYPE.get(work.type)
    ids, container_ids = [], []
    for identifier in work.ids:
        scheme = identifier.split(":", 1)[0]
        # ISSN, and the ISBN of anything but a whole book, name the container.
        if scheme == "issn" or (scheme == "isbn" and work.type != "book"):
            container_ids.append(identifier)
        elif allowed is None or scheme in allowed:
            ids.append(identifier)
    venue = " ".join(filter(None, [
        _venue_title(work.venue), f"[{' '.join(container_ids)}]" if container_ids else ""
    ]))
    row = {
        "id": " ".join(ids) or f"temp:{work.records[0]}",
        "title": work.title,
        "author": "; ".join(work.authors),
        "pub_date": work.pub_date,
        "venue": venue,
        "volume": _text(work.volume) if venue else "",
        "issue": _text(work.issue) if venue else "",
        "page": work.page,
        "type": work.type,
        "publisher": work.publisher,
        "editor": "; ".join(work.editors),
    }
    return row, ("" if ids else _missing(row))


def _missing(row: Mapping[str, str]) -> str:
    """What a temp-id row lacks under oc_validator's mandatory-field rules."""
    needed = []
    if row["type"] in _NEEDS_DATE_AND_AGENT:
        needed = ["title", "pub_date"] + ([] if row["author"] or row["editor"] else ["author or editor"])
    elif row["type"] in _NEEDS_VENUE:
        needed = ["title", "venue"]
    lacking = [name for name in needed if " or " in name or not row[name]]
    if not lacking:
        return ""
    type_name = row["type"] or "untyped"
    return f"no identifier, and a {type_name} row needs {', '.join(lacking)}"


def _citing_row(citing: CitingDocument | Mapping[str, Any]) -> dict[str, str]:
    if isinstance(citing, Mapping):
        row, reason = _meta_row(_work(0, citing, []))  # temp:0 when it has no id
        if reason:
            raise ValueError(f"Citing document metadata: {reason}")
        return row
    ids = []
    for raw in citing.ids.split():
        scheme, sep, value = raw.partition(":")
        is_doi = not sep or scheme.startswith("10.") or "doi.org/" in raw
        ids.append(_normalize_id("doi", raw) if is_doi else _normalize_id(scheme.lower(), value))
    if citing.ids.strip() and not all(ids):
        raise ValueError(f"Unsupported citing document id: {citing.ids!r}")
    row = {name: "" for name in META_FIELDS}
    row.update(
        id=" ".join(ids) or "temp:0",
        title=_text(citing.title),
        author="; ".join(_people(citing.authors)),
        pub_date=_text(citing.pub_date),
        type=_text(citing.type),
        venue=_venue_title(citing.venue),
    )
    if row["pub_date"] and not _DATE.match(row["pub_date"]):
        raise ValueError(f"Citing pub_date must be YYYY, YYYY-MM or YYYY-MM-DD: {citing.pub_date!r}")
    if not ids and _missing(row):
        raise ValueError("The citing document needs an id, or a title, pub_date and authors")
    return row


# --- text helpers -------------------------------------------------------------


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _venue_title(value: str) -> str:
    return _text(re.sub(r"[\[\]]", " ", value or ""))


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _write_csv(fields: Sequence[str], rows: Iterable[Mapping[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()

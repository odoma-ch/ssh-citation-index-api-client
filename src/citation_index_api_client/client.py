from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

import httpx

from citation_index_api_client.exceptions import APIError, JobFailedError, JobTimeoutError
from citation_index_api_client.models import JobResponse, JobStatus, LinkedReference
from citation_index_api_client.opencitations import (
    CitingDocument,
    OpenCitationsDeposit,
    to_opencitations,
)

DEFAULT_BASE_URL = (
    "https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl"
)
LINKING_TARGETS = ("openalex", "matilda", "wikidata", "opencitations")
API_KEY_ENV = "CITATION_INDEX_API_KEY"
API_KEY_HEADER = "X-API-Key"
TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def is_linkable(record: Mapping[str, Any]) -> bool:
    """Whether a parsed record has the fields the linking endpoint requires.

    ``POST /link/references`` rejects the whole batch with a 422 if any record
    is missing a non-empty ``full_title``, a non-empty ``authors`` list, or an
    integer ``publication_year`` (1–9999) — yet the parser routinely emits
    records with those fields ``None`` (e.g. "op. cit." citations). Filter with
    this before submitting so the linkable records still get through.
    """
    title = record.get("full_title")
    authors = record.get("authors")
    year = record.get("publication_year")
    return bool(
        isinstance(title, str)
        and title.strip()
        and isinstance(authors, list)
        and authors
        and isinstance(year, int)
        and 1 <= year <= 9999
    )


def _drop_none(params: Mapping[str, Any]) -> dict[str, Any]:
    # httpx sends None as an empty value, which fails enum validation server-side.
    return {k: v for k, v in params.items() if v is not None}


class CitationIndexClient:
    """Sync client for the Citation Index REST API.

    Example::

        client = CitationIndexClient(api_key="...")  # or set CITATION_INDEX_API_KEY
        job = client.extract_text("paper.pdf")
        result = client.wait_for_result(job.job_id)
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        api_key = api_key or os.environ.get(API_KEY_ENV)
        # Sent per request rather than baked into the httpx client, so the key
        # also reaches an injected client without mutating it.
        self._auth_headers = {API_KEY_HEADER: api_key} if api_key else {}
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers=dict(headers or {}),
        )

    def __enter__(self) -> CitationIndexClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def health(self) -> dict[str, Any]:
        """GET /health — API health check."""
        return self._get_json("/health")

    def root(self) -> dict[str, Any]:
        """GET / — API metadata."""
        return self._get_json("/")

    def get_job_status(self, job_id: str) -> JobStatus:
        """GET /jobs/{job_id}/status — poll job progress."""
        data = self._get_json(f"/jobs/{job_id}/status")
        return JobStatus.from_dict(data)

    def get_job_result(
        self,
        job_id: str,
        *,
        format: str = "json",
    ) -> Any:
        """GET /jobs/{job_id} — fetch job output (parsed JSON or raw XML text)."""
        response = self._request(
            "GET",
            f"/jobs/{job_id}",
            params={"format": format},
        )
        if format == "xml":
            return response.text
        return response.json()

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> JobStatus:
        """Poll until the job reaches a terminal state (``completed`` or ``failed``)."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_job_status(job_id)
            if status.is_terminal:
                if status.status == "failed":
                    raise JobFailedError(job_id, status.error)
                return status
            if time.monotonic() >= deadline:
                raise JobTimeoutError(job_id, timeout)
            time.sleep(poll_interval)

    def wait_for_result(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
        format: str = "json",
    ) -> Any:
        """Wait for job completion, then return the job result."""
        self.wait_for_job(job_id, poll_interval=poll_interval, timeout=timeout)
        return self.get_job_result(job_id, format=format)

    def extract_text(
        self,
        file: str | Path | BinaryIO,
        *,
        extractor: str = "pymupdf",
        markdown: bool = True,
        filename: str | None = None,
    ) -> JobResponse:
        """POST /extract/text — enqueue PDF text extraction."""
        return self._upload(
            "/extract/text",
            file,
            filename=filename,
            params={"extractor": extractor, "markdown": markdown},
        )

    def extract_references(
        self,
        text: str,
        *,
        method: str | None = None,
        prompt_name: str = "prompts/reference_extraction.md",
        temperature: float = 0.3,
    ) -> JobResponse:
        """POST /extract/references — enqueue LLM reference extraction from markdown.

        ``method`` is ``full_text`` (server default) or ``semantic``.
        """
        data = self._post_json(
            "/extract/references",
            json={"text": text},
            params=_drop_none(
                {
                    "method": method,
                    "prompt_name": prompt_name,
                    "temperature": temperature,
                }
            ),
        )
        return JobResponse.from_dict(data)

    def extract_references_from_pdf(
        self,
        file: str | Path | BinaryIO,
        *,
        include_parsed: bool = False,
        include_context: bool = False,
        filename: str | None = None,
    ) -> JobResponse:
        """POST /extract/references — enqueue GROBID reference extraction from a PDF.

        ``include_parsed`` adds ``parsed_references``; ``include_context`` adds
        the citing sentences of each reference.
        """
        return self._upload(
            "/extract/references",
            file,
            filename=filename,
            params={
                "method": "grobid",
                "include_parsed": include_parsed,
                "include_context": include_context,
            },
        )

    def process_references_grobid(
        self,
        file: str | Path | BinaryIO,
        *,
        include_context: bool = False,
        filename: str | None = None,
    ) -> JobResponse:
        """POST /process/references/grobid — PDF to raw and parsed references.

        The result holds index-aligned ``references``, ``parsed_references`` and,
        with ``include_context``, ``contexts`` ready for :meth:`classify_citations`.
        """
        return self._upload(
            "/process/references/grobid",
            file,
            filename=filename,
            params={"include_context": include_context},
        )

    def extract_metadata(
        self,
        file: str | Path | BinaryIO,
        *,
        method: str = "grobid",
        filename: str | None = None,
    ) -> JobResponse:
        """POST /extract/metadata — enqueue metadata extraction of the PDF itself.

        The result is ``{"metadata": {...}, "method": ...}``; ``metadata`` has the
        shape of a parsed reference and can be passed as ``citing`` to
        :func:`~citation_index_api_client.to_opencitations`. ``method`` is
        ``grobid`` (TEI header; fast when the same PDF already went through a
        GROBID endpoint) or ``llm`` (first pages; better authors, fills
        ``journal_title`` / ``publisher``). No metadata found fails the job.
        """
        return self._upload("/extract/metadata", file, filename=filename, params={"method": method})

    def parse_references(
        self,
        references: list[str],
        *,
        parser: str = "llm",
        prompt_name: str = "prompts/reference_parsing.md",
        temperature: float = 0.0,
    ) -> JobResponse:
        """POST /parse/references — enqueue parsing of reference strings."""
        data = self._post_json(
            "/parse/references",
            json={"references": references},
            params={
                "parser": parser,
                "prompt_name": prompt_name,
                "temperature": temperature,
            },
        )
        return JobResponse.from_dict(data)

    def link_references(
        self,
        references: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        target: str | Sequence[str] = "all",
        method: str = "match",
        filter_reviews_by_doi: bool = False,
    ) -> JobResponse:
        """POST /link/references — enqueue linking against external indexes.

        ``method`` is ``match`` (title/author/year rules) or ``model`` (the
        fine-tuned decider picks among the search results; every record then
        needs ``raw_reference``, the original citation string).
        ``filter_reviews_by_doi`` drops candidates whose DOI only names a book
        review before choosing.
        """
        # A dict is a Sequence-like iterable, so it must be matched before the
        # list branch — otherwise a single record would be iterated by key.
        if isinstance(references, dict):
            batched = False
            reference: Any = references
        else:
            entries = [item for item in references if item]
            if not entries:
                raise ValueError("At least one non-empty reference is required")
            batched = True
            reference = entries

        data = self._post_json(
            "/link/references",
            json={"reference": reference},
            params={
                "batched": batched,
                "target": target if isinstance(target, str) else ",".join(target),
                "method": method,
                "filter_reviews_by_doi": filter_reviews_by_doi,
            },
        )
        return JobResponse.from_dict(data)

    def classify_citations(
        self,
        citations: Sequence[Mapping[str, Any]],
        *,
        mode: str = "M",
    ) -> JobResponse:
        """POST /classify/citations — enqueue citation-intent classification.

        Each citation is ``{"section": ..., "citation": ...}`` (section may be
        ``""``); GROBID ``contexts`` can be passed as-is. ``mode`` is ``WS``
        (with sections), ``WoS`` (without) or ``M`` (per item).
        """
        entries = [item for item in citations if item]
        if not entries:
            raise ValueError("At least one citation is required")
        data = self._post_json(
            "/classify/citations",
            json={"citations": entries},
            params={"mode": mode},
        )
        return JobResponse.from_dict(data)

    def wait_for_links(
        self,
        job_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> list[LinkedReference]:
        """Wait for a linking job, then return its results as dataclasses."""
        result = self.wait_for_result(
            job_id, poll_interval=poll_interval, timeout=timeout
        )
        return [LinkedReference.from_dict(item) for item in result.get("results", [])]

    def opencitations_deposit(
        self,
        file: str | Path,
        *,
        citing: CitingDocument | Mapping[str, Any] | None = None,
        metadata_method: str = "grobid",
        link: bool = True,
        target: str | Sequence[str] = "all",
        link_method: str = "match",
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> OpenCitationsDeposit:
        """PDF or Markdown file to an OpenCitations deposit, end to end.

        A PDF goes through ``/process/references/grobid``; a ``.md`` /
        ``.markdown`` / ``.txt`` file through LLM extraction and parsing. The
        records that have a title, authors and a year are then linked (unless
        ``link=False``). Without ``citing``, a PDF's own row comes from
        ``/extract/metadata`` (``metadata_method``); a Markdown file needs
        ``citing``. If the API finds no metadata, ``JobFailedError``
        is raised: pass ``citing`` then. Write ``deposit.text`` to a file to deposit it.
        """
        path = Path(file)
        wait = {"poll_interval": poll_interval, "timeout": timeout}
        if path.suffix.lower() in TEXT_SUFFIXES:
            if citing is None:
                raise ValueError("A Markdown file needs citing=: there is no PDF to read metadata from")
            text = path.read_text(encoding="utf-8")
            found = self.wait_for_result(self.extract_references(text).job_id, **wait)
            strings = found.get("references") or []
            parsed = self.wait_for_result(self.parse_references(strings).job_id, **wait) if strings else {}
            records = parsed.get("references") or []
        else:
            result = self.wait_for_result(self.process_references_grobid(path).job_id, **wait)
            records = result.get("parsed_references") or []
            if citing is None:
                # After the references, so the server reuses its cached GROBID run.
                job = self.extract_metadata(path, method=metadata_method)
                citing = self.wait_for_result(job.job_id, **wait)["metadata"]

        links: list[LinkedReference | None] = [None] * len(records)
        numbered = [(i, r) for i, r in enumerate(records) if r and is_linkable(r)]
        if link and numbered:
            job = self.link_references([r for _, r in numbered], target=target, method=link_method)
            for (i, _), linked in zip(numbered, self.wait_for_links(job.job_id, **wait)):
                links[i] = linked
        return to_opencitations(records, links, citing=citing)

    def _upload(
        self,
        path: str,
        file: str | Path | BinaryIO,
        *,
        filename: str | None,
        params: Mapping[str, Any],
    ) -> JobResponse:
        if isinstance(file, (str, Path)):
            with Path(file).open("rb") as fh:
                return self._upload(
                    path, fh, filename=filename or Path(file).name, params=params
                )
        name = filename or getattr(file, "name", None) or "upload.pdf"
        response = self._request(
            "POST",
            path,
            params=params,
            files={"file": (Path(str(name)).name, file, "application/pdf")},
        )
        return JobResponse.from_dict(response.json())

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", path, **kwargs).json()

    def _post_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", path, **kwargs).json()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._auth_headers, **kwargs.pop("headers", {})}
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.is_success:
            return response
        detail: Any | None
        try:
            detail = response.json()
        except ValueError:
            detail = response.text or None
        raise APIError(
            f"{method} {path} failed with status {response.status_code}",
            status_code=response.status_code,
            detail=detail,
        )

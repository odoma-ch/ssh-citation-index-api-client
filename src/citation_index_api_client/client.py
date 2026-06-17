from __future__ import annotations

import time
from pathlib import Path
from typing import Any, BinaryIO, Mapping

import httpx

from citation_index_api_client.exceptions import APIError, JobFailedError, JobTimeoutError
from citation_index_api_client.models import JobResponse, JobStatus

DEFAULT_BASE_URL = (
    "https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl"
)


class CitationIndexClient:
    """Sync client for the Citation Index REST API.

    Example::

        client = CitationIndexClient()
        job = client.extract_text("paper.pdf")
        result = client.wait_for_result(job.job_id)
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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
        if isinstance(file, (str, Path)):
            path = Path(file)
            with path.open("rb") as fh:
                return self._enqueue_text_upload(
                    fh,
                    extractor=extractor,
                    markdown=markdown,
                    filename=filename or path.name,
                )
        name = filename or getattr(file, "name", "upload.pdf")
        if isinstance(name, (str, Path)):
            name = Path(name).name
        return self._enqueue_text_upload(
            file,
            extractor=extractor,
            markdown=markdown,
            filename=str(name),
        )

    def extract_references(
        self,
        text: str,
        *,
        method: str = "full_text",
        prompt_name: str = "prompts/reference_extraction.md",
        temperature: float = 0.3,
    ) -> JobResponse:
        """POST /extract/references — enqueue reference extraction from markdown."""
        data = self._post_json(
            "/extract/references",
            json={"text": text},
            params={
                "method": method,
                "prompt_name": prompt_name,
                "temperature": temperature,
            },
        )
        return JobResponse.from_dict(data)

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

    def _enqueue_text_upload(
        self,
        file_obj: BinaryIO,
        *,
        extractor: str,
        markdown: bool,
        filename: str,
    ) -> JobResponse:
        response = self._request(
            "POST",
            "/extract/text",
            params={"extractor": extractor, "markdown": markdown},
            files={"file": (filename, file_obj, "application/pdf")},
        )
        return JobResponse.from_dict(response.json())

    def _get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", path, **kwargs).json()

    def _post_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", path, **kwargs).json()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
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

import json

import httpx
import pytest

from citation_index_api_client import (
    APIError,
    CitationIndexClient,
    JobFailedError,
    JobTimeoutError,
    is_linkable,
)
from citation_index_api_client.models import JobResponse, JobStatus, ReferenceLink


@pytest.fixture
def link_requests():
    return []


@pytest.fixture
def transport(link_requests):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/":
            return httpx.Response(200, json={"title": "Citation Index API"})
        if request.url.path == "/jobs/abc/status":
            return httpx.Response(
                200,
                json={"job_id": "abc", "status": "completed", "progress": 100},
            )
        if request.url.path == "/jobs/abc":
            return httpx.Response(200, json={"text": "hello"})
        if request.url.path == "/extract/references":
            return httpx.Response(
                200,
                json={
                    "job_id": "ref-1",
                    "status": "queued",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/parse/references":
            return httpx.Response(
                200,
                json={
                    "job_id": "parse-1",
                    "status": "queued",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/link/references":
            link_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "job_id": "link-1",
                    "status": "queued",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            )
        if request.url.path == "/jobs/link-1/status":
            return httpx.Response(200, json={"job_id": "link-1", "status": "completed"})
        if request.url.path == "/jobs/link-1":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "reference": {
                                "full_title": "Title",
                                "authors": [{"surname": "Smith"}],
                                "publication_year": 2020,
                            },
                            "links": {
                                "openalex": {
                                    "id": "https://openalex.org/W1",
                                    "doi": "10.1234/abc",
                                },
                                "wikidata": {"id": None, "doi": None},
                            },
                        }
                    ]
                },
            )
        if request.url.path == "/extract/text":
            return httpx.Response(
                200,
                json={
                    "job_id": "txt-1",
                    "status": "queued",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def client(transport):
    http = httpx.Client(transport=transport, base_url="https://api.example")
    wrapper = CitationIndexClient(base_url="https://api.example", client=http)
    yield wrapper
    wrapper.close()


def test_health(client):
    assert client.health() == {"status": "ok"}


def test_get_job_status(client):
    status = client.get_job_status("abc")
    assert status == JobStatus(job_id="abc", status="completed", progress=100)
    assert status.is_terminal


def test_get_job_result(client):
    assert client.get_job_result("abc") == {"text": "hello"}


def test_extract_references(client):
    job = client.extract_references("# refs\n- Smith 2020")
    assert job.job_id == "ref-1"
    assert isinstance(job, JobResponse)


def test_parse_references(client):
    job = client.parse_references(["Smith, J. (2020). Title."])
    assert job.job_id == "parse-1"


RECORD = {"full_title": "Title", "authors": [{"surname": "Smith"}], "publication_year": 2020}


def test_link_references_single(client, link_requests):
    job = client.link_references(RECORD)
    assert job.job_id == "link-1"
    assert isinstance(job, JobResponse)
    request = link_requests[-1]
    assert request.url.params["batched"] == "false"
    assert request.url.params["target"] == "all"
    assert json.loads(request.content) == {"reference": RECORD}


def test_link_references_batched(client, link_requests):
    second = {"full_title": "Other", "authors": [{"surname": "Doe"}], "publication_year": 2021}
    client.link_references(
        [RECORD, second],
        target=["openalex", "wikidata"],
    )
    request = link_requests[-1]
    assert request.url.params["batched"] == "true"
    assert request.url.params["target"] == "openalex,wikidata"
    assert json.loads(request.content) == {"reference": [RECORD, second]}


def test_is_linkable():
    assert is_linkable(RECORD)
    # Missing / null required fields — all rejected by the linking endpoint.
    assert not is_linkable({"full_title": None, "authors": [{"surname": "X"}], "publication_year": 2020})
    assert not is_linkable({"full_title": "   ", "authors": [{"surname": "X"}], "publication_year": 2020})
    assert not is_linkable({"full_title": "T", "authors": None, "publication_year": 2020})
    assert not is_linkable({"full_title": "T", "authors": [], "publication_year": 2020})
    assert not is_linkable({"full_title": "T", "authors": [{"surname": "X"}], "publication_year": None})
    assert not is_linkable({"full_title": "T", "authors": [{"surname": "X"}], "publication_year": 12345})


def test_link_references_rejects_empty(client):
    with pytest.raises(ValueError):
        client.link_references([])
    with pytest.raises(ValueError):
        client.link_references([None])


def test_wait_for_links(client):
    job = client.link_references(RECORD)
    linked = client.wait_for_links(job.job_id, poll_interval=0.01)
    assert len(linked) == 1
    assert linked[0].reference == {
        "full_title": "Title",
        "authors": [{"surname": "Smith"}],
        "publication_year": 2020,
    }
    assert linked[0].links["openalex"] == ReferenceLink(
        id="https://openalex.org/W1",
        doi="10.1234/abc",
    )
    assert linked[0].links["openalex"].is_linked
    assert not linked[0].links["wikidata"].is_linked


def test_extract_text_file(tmp_path, client):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    job = client.extract_text(pdf)
    assert job.job_id == "txt-1"


def test_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"msg": "bad"}]})

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example",
    )
    client = CitationIndexClient(client=http)
    with pytest.raises(APIError) as exc:
        client.health()
    assert exc.value.status_code == 422
    client.close()


def test_wait_for_job_failed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"job_id": "x", "status": "failed", "error": "boom"},
        )

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example",
    )
    client = CitationIndexClient(client=http)
    with pytest.raises(JobFailedError, match="boom"):
        client.wait_for_job("x", poll_interval=0.01, timeout=1.0)
    client.close()


def test_wait_for_job_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "x", "status": "processing"})

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.example",
    )
    client = CitationIndexClient(client=http)
    with pytest.raises(JobTimeoutError):
        client.wait_for_job("x", poll_interval=0.01, timeout=0.05)
    client.close()

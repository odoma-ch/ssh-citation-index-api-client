# Citation Index API Client

Python client for the [Citation Index API](https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl/openapi.json), which extracts and parses citations from academic PDFs.

## Install

```bash
pip install -e .
# or with dev dependencies
pip install -e ".[dev]"
```

## Quick start

```python
from citation_index_api_client import CitationIndexClient

client = CitationIndexClient()  # staging URL by default

# Health check
print(client.health())

# Extract text from a PDF (async job)
job = client.extract_text("paper.pdf", extractor="pymupdf", markdown=True)
text_result = client.wait_for_result(job.job_id)

# Extract references from markdown
job = client.extract_references(markdown_text)
refs = client.wait_for_result(job.job_id)

# Parse raw reference strings
job = client.parse_references(
    ["Smith, J. (2020). Title. Journal."],
    parser="llm",
)
parsed = client.wait_for_result(job.job_id)
```

Use a context manager to ensure the HTTP connection is closed:

```python
with CitationIndexClient(base_url="https://your-api.example") as client:
    job = client.parse_references(["..."])
    status = client.get_job_status(job.job_id)
```

## API coverage

| Method | Endpoint | Client method |
|--------|----------|---------------|
| GET | `/health` | `health()` |
| GET | `/` | `root()` |
| GET | `/jobs/{job_id}/status` | `get_job_status(job_id)` |
| GET | `/jobs/{job_id}` | `get_job_result(job_id, format="json")` |
| POST | `/extract/text` | `extract_text(file, ...)` |
| POST | `/extract/references` | `extract_references(text, ...)` |
| POST | `/parse/references` | `parse_references(references, ...)` |

Helper methods:

- `wait_for_job(job_id)` — poll until `completed` or `failed`
- `wait_for_result(job_id)` — poll, then return the job result

## Configuration

- **`base_url`** — API root (default: staging deployment)
- **`timeout`** — per-request HTTP timeout in seconds
- **`headers`** — extra headers (e.g. auth tokens if required)
- **`client`** — inject a custom `httpx.Client` for testing or advanced setup

## Errors

- `APIError` — non-2xx HTTP responses (`status_code`, `detail`)
- `JobFailedError` — job finished with `status == "failed"`
- `JobTimeoutError` — `wait_for_job` / `wait_for_result` exceeded `timeout`

## Interactive notebook

```bash
pip install ipykernel
jupyter notebook notebooks/explore_client.ipynb
```

## Tests

```bash
pytest
```

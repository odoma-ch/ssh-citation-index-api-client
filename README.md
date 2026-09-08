# Citation Index API Client

Python client for the [Citation Index API](https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl/openapi.json), which extracts and parses citations from academic PDFs and links them to external scholarly indexes.

The client version tracks the API version it targets: **0.3.0**.

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

# Link references to external scholarly indexes
job = client.link_references(
    ["Smith, J. (2020). Title. Journal. doi:10.1234/abc"],
    target="all",  # or "openalex", "wikidata", "openalex,matilda"
)
for item in client.wait_for_links(job.job_id):
    print(item.reference, item.links["openalex"].id)
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
| POST | `/link/references` | `link_references(references, target="all")` |

Helper methods:

- `wait_for_job(job_id)` — poll until `completed` or `failed`
- `wait_for_result(job_id)` — poll, then return the job result
- `wait_for_links(job_id)` — poll, then return the linking result as `LinkedReference` objects

## Citation linking

`link_references` accepts either a single reference string or a list, in which case
the client submits it as a newline-delimited batch (`batched=true`). Supported
targets are `openalex`, `matilda` and `wikidata`, passed as `"all"`, a single name,
a comma-separated string, or a list of names. OpenCitations is deliberately not
a linking target.

Results come back as one `LinkedReference` per submitted reference, each holding a
`links` mapping from target name to a `ReferenceLink` with `id` and `doi`:

```python
job = client.link_references(raw_references, target="openalex,wikidata")
for item in client.wait_for_links(job.job_id):
    for target, link in item.links.items():
        print(target, link.id, link.doi, link.is_linked)
```

An `id` and `doi` of `None` means the target could not make a confident match —
the service requires a DOI hit or a near-exact title match — not that the job
failed. Matilda may need deployment-specific credentials configured server-side.
Linking makes live external API calls once per reference per target, so submit
bounded batches and raise `timeout` accordingly.

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

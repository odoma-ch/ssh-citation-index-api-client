# Citation Index API Client

Python client for the [Citation Index API](https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl/openapi.json), which extracts and parses citations from academic PDFs and links them to external scholarly indexes.

The client version tracks the API version it targets: **0.3.1**.

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

# Parse raw reference strings into structured records
from citation_index_api_client import is_linkable

job = client.parse_references(
    ["Smith, J. (2020). Title. Journal. doi:10.1234/abc"],
    parser="llm",
)
records = client.wait_for_result(job.job_id)["references"]

# Link references to external scholarly indexes. Linking takes the structured
# records that parse_references produces, not raw strings. The endpoint rejects
# the whole batch if any record lacks a title, authors or year, so filter first.
job = client.link_references(
    [r for r in records if is_linkable(r)],
    target="all",  # or "openalex", "wikidata", "openalex,matilda"
)
for item in client.wait_for_links(job.job_id):
    print(item.reference["full_title"], item.links["openalex"].id)
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

As of API 0.3.1 `link_references` takes structured `LinkingReference` records —
the mappings `parse_references` returns — rather than raw strings. Pass a single
record mapping, or a list, which the client submits as a batch (`batched=true`).
Supported targets are `openalex`, `matilda` and `wikidata`, passed as `"all"`, a
single name, a comma-separated string, or a list of names. OpenCitations is
deliberately not a linking target.

Results come back as one `LinkedReference` per submitted record. Each holds the
submitted record echoed back in `reference` (a mapping, no longer a string) and a
`links` mapping from target name to a `ReferenceLink` with `id` and `doi`:

```python
records = client.wait_for_result(parse_job.job_id)["references"]
job = client.link_references(
    [r for r in records if is_linkable(r)], target="openalex,wikidata"
)
for item in client.wait_for_links(job.job_id):
    print(item.reference["full_title"])
    for target, link in item.links.items():
        print(target, link.id, link.doi, link.is_linked)
```

Linking requires each record to carry a non-empty `full_title`, at least one
`authors` entry, and an integer `publication_year`; the endpoint returns 422 for
the whole batch if any record is missing them. The parser regularly emits records
with those fields `None` (e.g. "op. cit." citations), so pass records through
`is_linkable` and drop the rest before submitting.

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

## Web interface

A read-only [Gradio](https://www.gradio.app/) front-end makes the API usable
without writing Python. Gradio requires Python 3.10 or newer, so it ships as an
optional extra:

```bash
pip install -e ".[app]"
python app/gradio_app.py
```

The interface opens on <http://127.0.0.1:7860> with three tabs:

- **From a document** — runs extraction, parsing and linking in sequence,
  streaming the status of each stage. Upload a PDF and pick an extractor
  (`mineru` or `pymupdf`), or paste Markdown you already have to skip text
  extraction entirely. The parser dropdown selects `llm` or `grobid`
- **From reference strings** — links pasted references without a document
- **Look up a job** — shows the status and result of a job ID, so a long
  extraction survives closing the browser

By default only the first few extracted references are linked, since every
reference is looked up once per selected target against live external services.
Tick **Link every extracted reference** to process them all, or use the slider
for anything in between.

The reference and link tables are read-only and downloadable as CSV. Structured
records from the parser are download-only — they are too wide to read in a
browser table. Configure the interface through environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CITATION_INDEX_API` | staging URL | API base URL |
| `CITATION_INDEX_JOB_TIMEOUT` | `1800` | seconds before a job is abandoned |
| `CITATION_INDEX_POLL_INTERVAL` | `2` | seconds between status polls |
| `CITATION_INDEX_LINKING_LIMIT` | `25` | starting position of the "references to link" slider |
| `CITATION_INDEX_LINKING_MAX` | `500` | ceiling on references per linking batch |
| `CITATION_INDEX_UI_AUTH` | unset | `user:password` login gate |
| `CITATION_INDEX_UI_HOST` / `_PORT` | `127.0.0.1` / `7860` | bind address |
| `CITATION_INDEX_UI_MAX_UPLOAD` | `50mb` | upload size cap |

Set `CITATION_INDEX_UI_AUTH` before exposing the interface beyond localhost —
the API itself is unauthenticated, and the extraction and parsing endpoints are
LLM-backed.

## Interactive notebook

```bash
pip install ipykernel
jupyter notebook notebooks/explore_client.ipynb
```

## Tests

```bash
pytest
```

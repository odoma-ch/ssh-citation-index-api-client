# Citation Index API Client

Python client for the [Citation Index API](https://citation-index-api-graphia-app1-staging.apps.bst2.paas.psnc.pl/openapi.json), which extracts and parses citations from academic PDFs and links them to external scholarly indexes.

The client version tracks the API version it targets: **1.0.0-beta.1** (Python package version `1.0.0b1`).

## Install

```bash
pip install -e .
# or with dev dependencies
pip install -e ".[dev]"
```

## Quick start

```python
from citation_index_api_client import CitationIndexClient

# Every endpoint needs an API key, sent as the X-API-Key header. Pass it
# explicitly or set CITATION_INDEX_API_KEY in the environment.
client = CitationIndexClient(api_key="your-key")  # staging URL by default

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
    target="all",  # or "openalex", "wikidata", "openalex,opencitations"
)
for item in client.wait_for_links(job.job_id):
    print(item.reference["full_title"], item.links["openalex"].id)
```

## GROBID pipeline and citation classification

API 0.4 adds a GROBID path that goes straight from a PDF to references, plus
citation-intent classification (method / background / result):

```python
# PDF -> index-aligned raw references, parsed records and citing contexts
job = client.process_references_grobid("paper.pdf", include_context=True)
result = client.wait_for_result(job.job_id)
references, parsed = result["references"], result["parsed_references"]

# Contexts ({section, citation, ...}) go straight to the classifier.
# mode: "WS" (with sections), "WoS" (without) or "M" (per item, default)
job = client.classify_citations(result["contexts"], mode="M")
intents = client.wait_for_result(job.job_id)

# /extract/references also accepts a PDF upload, which uses GROBID
job = client.extract_references_from_pdf(
    "paper.pdf", include_parsed=True, include_context=True
)
```

`extract_references(text)` stays the LLM path for Markdown; its `method` is
`full_text` (server default) or `semantic`.

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
| POST | `/extract/references` | `extract_references(text, ...)` (LLM, Markdown) |
| POST | `/extract/references` | `extract_references_from_pdf(file, ...)` (GROBID, PDF) |
| POST | `/process/references/grobid` | `process_references_grobid(file, include_context=False)` |
| POST | `/extract/metadata` | `extract_metadata(file, method="grobid")` |
| POST | `/parse/references` | `parse_references(references, ...)` |
| POST | `/link/references` | `link_references(references, target="all", method="match", filter_reviews_by_doi=False)` |
| POST | `/classify/citations` | `classify_citations(citations, mode="M")` |

Helper methods:

- `wait_for_job(job_id)` — poll until `completed` or `failed`
- `wait_for_result(job_id)` — poll, then return the job result
- `wait_for_links(job_id)` — poll, then return the linking result as `LinkedReference` objects

## Citation linking

As of API 0.3.1 `link_references` takes structured `LinkingReference` records —
the mappings `parse_references` returns — rather than raw strings. Pass a single
record mapping, or a list, which the client submits as a batch (`batched=true`).
Supported targets are `openalex`, `matilda`, `wikidata` and (since API 0.4)
`opencitations`, passed as `"all"`, a single name, a comma-separated string, or a
list of names.

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
failed. A target whose lookup failed sets `ReferenceLink.error` instead.

Since API 0.5, `method="model"` lets a fine-tuned decider choose among each
index's search results instead of the title/author/year rules; every record then
needs `raw_reference` (the original citation string), and links carry a `score`.
`filter_reviews_by_doi=True` drops candidates whose DOI only names a book review. Matilda may need deployment-specific credentials configured server-side.
Linking makes live external API calls once per reference per target, so submit
bounded batches and raise `timeout` accordingly.

## OpenCitations export

`to_opencitations` turns parsed references (plus, optionally, their linking
results) into an [OpenCitations crowdsourcing](https://github.com/opencitations/crowdsourcing)
deposit: META-CSV, the `===###===@@@===` line, then CITS-CSV. Linked references
get `doi:`, `openalex:`, `wikidata:` and `omid:` ids; the rest get `temp:` ids.
Repeated footnote references are merged, short forms (ibid., op. cit.) and rows
[oc_validator](https://github.com/opencitations/oc_validator) would reject are
dropped and listed in `deposit.dropped`.

One call runs the whole pipeline (no UI needed): references, linking of the records
that have a title, authors and year, the citing paper's metadata, and the deposit:

```python
from pathlib import Path
from citation_index_api_client import CitationIndexClient, CitingDocument

with CitationIndexClient() as client:                      # CITATION_INDEX_API_KEY from env
    deposit = client.opencitations_deposit("paper.pdf")    # metadata_method="llm" for a fuller citing row
    # Markdown has no PDF to read metadata from, so name the citing paper:
    # client.opencitations_deposit("paper.md", citing=CitingDocument(ids="doi:10.1234/abc"))
Path("paper-opencitations.csv").write_text(deposit.text, encoding="utf-8")
print(len(deposit.meta) - 1, "works,", len(deposit.dropped), "dropped:", deposit.dropped)
```

Options: `link=False`, `target=["openalex", "wikidata"]`, `link_method="model"`,
`citing=` to skip `/extract/metadata`. The steps one by one, if you need them:

```python
from pathlib import Path
from citation_index_api_client import to_opencitations

refs = client.wait_for_result(client.process_references_grobid("paper.pdf").job_id)
# After the references, so the server reuses its cached GROBID run (~3 s).
# method="llm" (~20 s) gets authors right more often and fills the journal / publisher.
meta = client.wait_for_result(client.extract_metadata("paper.pdf", method="grobid").job_id)

records = refs["parsed_references"]
links = [None] * len(records)                   # index-aligned with records
for n, item in zip(linked_numbers, linked):     # 1-based numbers of linked records
    links[n - 1] = item
deposit = to_opencitations(records, links, citing=meta["metadata"])
Path("deposit.csv").write_text(deposit.text)
```

If no metadata is found the job fails (`JobFailedError`); then type the citing paper
in: `CitingDocument(ids="doi:10.1234/abc")`, or without a DOI
`CitingDocument(title=..., pub_date="2021", authors=["Surname, Given"])`.

`/extract/metadata` needs API 0.6.0 or later.

Validate before depositing: `pip install oc-validator` (part of the `dev` extra).

## Configuration

- **`base_url`** — API root (default: staging deployment)
- **`api_key`** — sent as `X-API-Key`; falls back to `CITATION_INDEX_API_KEY`.
  Required by every endpoint since API 0.4 (401 without it)
- **`timeout`** — per-request HTTP timeout in seconds
- **`headers`** — extra headers
- **`client`** — inject a custom `httpx.Client` for testing or advanced setup

## Errors

- `APIError` — non-2xx HTTP responses (`status_code`, `detail`)
- `JobFailedError` — job finished with `status == "failed"`
- `JobTimeoutError` — `wait_for_job` / `wait_for_result` exceeded `timeout`

## Web interface

A [Streamlit](https://streamlit.io/) front-end makes every endpoint usable
without writing Python. It ships as an optional extra:

```bash
pip install -e ".[app]"
export CITATION_INDEX_API_KEY=...   # or enter the key in the sidebar
streamlit run app/streamlit_app.py
```

It opens on <http://localhost:8501>. The sidebar holds the API key (a key typed
there stays in that browser session) and shows the API name, version and docs
link (`GET /`) and whether the key is accepted. The API URL comes only from
`CITATION_INDEX_API`, so an environment key is never sent to another host. Pages:

**Workflow**

- **End-to-end pipeline** — upload a PDF or a Markdown file (Markdown skips
  text extraction and takes the LLM route); references are extracted, parsed,
  linked and their citing sentences classified, each stage showing its own
  status and job ID. Two routes:
  - *GROBID* (default) — one `/process/references/grobid` job returns raw
    references, parsed records and citing sentences, so every step runs
  - *LLM* — `/extract/text` (PyMuPDF or MinerU), then LLM reference
    extraction and parsing. No citing sentences, so no classification

  Results land in tabs: one table joining each reference's raw string, parsed
  fields and citation intents; links per target (IDs are clickable); citation
  intents per sentence; an OpenCitations deposit (CSV; the citing paper's row
  comes from `/extract/metadata`, GROBID or LLM, and can be typed in by hand
  instead, which a Markdown upload always needs); the document text; and everything
  as one JSON file.
  If a later stage fails, earlier results stay.

**Single steps** — each covers one endpoint and can hand its output to the next:

- **Extract** — PDF → text (`/extract/text`, any extractor); Markdown →
  references (`/extract/references`, LLM, `full_text` or `semantic`); PDF →
  references (`/extract/references` upload, GROBID, optionally parsed and with
  citing sentences)
- **Parse** — reference strings → structured records (`llm` or `grobid`)
- **Link** — reference strings (parsed first) or parsed records as JSON →
  OpenAlex, Matilda, Wikidata, OpenCitations. Records without a title, authors
  and year are skipped and counted, since the endpoint rejects the whole batch
- **Classify** — an editable table of section + citing sentence → method,
  background or result, with ensemble confidences
- **Jobs** — every job submitted in this browser session; look up any job ID
  for its status and result (`json` or `xml`)

Jobs keep running on the server when you switch pages. Returning to a page
resumes polling the same job instead of submitting it again, and a job ID can
always be looked up on **Jobs**. Reloading the browser tab starts a new
session, so results shown on pages are lost, but the jobs themselves are not.

Linking defaults to Matilda and sends every linkable reference in one batch.
Each reference is looked up once per target against live external services, and
Matilda takes roughly 20 seconds per reference, so a paper with dozens of
references takes several minutes. Set **Max references to link** to send fewer.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CITATION_INDEX_API` | staging URL | API base URL |
| `CITATION_INDEX_API_KEY` | unset | default API key (sidebar overrides it) |
| `CITATION_INDEX_JOB_TIMEOUT` | `1800` | seconds before the page stops waiting for a job |
| `CITATION_INDEX_POLL_INTERVAL` | `2` | seconds between status polls |
| `CITATION_INDEX_HTTP_TIMEOUT` | `120` | per-request HTTP timeout |
| `CITATION_INDEX_LINKING_LIMIT` | unset (all) | default "max references to link" |
| `CITATION_INDEX_LINKING_MAX` | `500` | ceiling on references per linking batch |
| `CITATION_INDEX_UI_AUTH` | unset | `user:password` login gate |

Host, port and upload size are Streamlit's own settings, e.g.
`STREAMLIT_SERVER_ADDRESS`, `STREAMLIT_SERVER_PORT` and
`STREAMLIT_SERVER_MAX_UPLOAD_SIZE` (MB), or the matching `--server.*` flags.

Set `CITATION_INDEX_UI_AUTH` before exposing the interface beyond localhost with
a key in the environment — anyone who can open the page then uses your key, and
the extraction and parsing endpoints are LLM-backed.

## Interactive notebook

Set `CITATION_INDEX_API_KEY` before starting Jupyter so the notebook's client
picks it up.

```bash
pip install ipykernel
jupyter notebook notebooks/explore_client.ipynb
```

## Tests

```bash
pytest
```

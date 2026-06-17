"""Python client for the Citation Index API."""

from citation_index_api_client.client import CitationIndexClient
from citation_index_api_client.exceptions import (
    APIError,
    JobFailedError,
    JobTimeoutError,
)
from citation_index_api_client.models import JobResponse, JobStatus

__all__ = [
    "APIError",
    "CitationIndexClient",
    "JobFailedError",
    "JobResponse",
    "JobStatus",
    "JobTimeoutError",
]

__version__ = "0.1.0"

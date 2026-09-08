"""Python client for the Citation Index API."""

from citation_index_api_client.client import CitationIndexClient
from citation_index_api_client.exceptions import (
    APIError,
    JobFailedError,
    JobTimeoutError,
)
from citation_index_api_client.models import (
    JobResponse,
    JobStatus,
    LinkedReference,
    ReferenceLink,
)

__all__ = [
    "APIError",
    "CitationIndexClient",
    "JobFailedError",
    "JobResponse",
    "JobStatus",
    "JobTimeoutError",
    "LinkedReference",
    "ReferenceLink",
]

__version__ = "0.3.0"

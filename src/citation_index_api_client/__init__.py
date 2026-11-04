"""Python client for the Citation Index API."""

from citation_index_api_client.client import (
    CitationIndexClient,
    LINKING_TARGETS,
    is_linkable,
)
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
    "LINKING_TARGETS",
    "LinkedReference",
    "ReferenceLink",
    "is_linkable",
]

__version__ = "0.4.2"

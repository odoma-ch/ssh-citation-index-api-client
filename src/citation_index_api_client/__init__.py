"""Python client for the Citation Index API."""

from citation_index_api_client.client import (
    LINKING_TARGETS,
    CitationIndexClient,
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
from citation_index_api_client.opencitations import (
    CitingDocument,
    OpenCitationsDeposit,
    to_opencitations,
)

__all__ = [
    "LINKING_TARGETS",
    "APIError",
    "CitationIndexClient",
    "CitingDocument",
    "JobFailedError",
    "JobResponse",
    "JobStatus",
    "JobTimeoutError",
    "LinkedReference",
    "OpenCitationsDeposit",
    "ReferenceLink",
    "is_linkable",
    "to_opencitations",
]

__version__ = "0.5.1"

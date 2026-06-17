from __future__ import annotations

from typing import Any


class APIError(Exception):
    """Raised when the API returns a non-success HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class JobFailedError(Exception):
    """Raised when a job completes with status ``failed``."""

    def __init__(self, job_id: str, error: str | None = None) -> None:
        self.job_id = job_id
        self.error = error
        message = f"Job {job_id} failed"
        if error:
            message = f"{message}: {error}"
        super().__init__(message)


class JobTimeoutError(TimeoutError):
    """Raised when waiting for a job exceeds the configured timeout."""

    def __init__(self, job_id: str, timeout: float) -> None:
        self.job_id = job_id
        self.timeout = timeout
        super().__init__(f"Job {job_id} did not complete within {timeout}s")

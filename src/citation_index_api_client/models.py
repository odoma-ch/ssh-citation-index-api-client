from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobResponse:
    """Response returned when a job is enqueued."""

    job_id: str
    status: str
    created_at: str
    message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobResponse:
        return cls(
            job_id=data["job_id"],
            status=data["status"],
            created_at=data["created_at"],
            message=data.get("message"),
        )


@dataclass(frozen=True)
class JobStatus:
    """Current state of an asynchronous job."""

    job_id: str
    status: str
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    current_stage: str | None = None
    completed_stages: list[Any] | None = None
    error: str | None = None
    progress: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobStatus:
        return cls(
            job_id=data["job_id"],
            status=data["status"],
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            current_stage=data.get("current_stage"),
            completed_stages=data.get("completed_stages"),
            error=data.get("error"),
            progress=data.get("progress"),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed")


@dataclass(frozen=True)
class ReferenceLink:
    """Identifier resolved for one reference in one target index.

    ``score`` is set by ``method="model"``; ``error`` when this target's lookup
    failed (the other targets' links are still returned).
    """

    id: str | None = None
    doi: str | None = None
    score: float | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReferenceLink:
        return cls(
            id=data.get("id"),
            doi=data.get("doi"),
            score=data.get("score"),
            error=data.get("error"),
        )

    @property
    def is_linked(self) -> bool:
        return self.id is not None


@dataclass(frozen=True)
class LinkedReference:
    """One reference and its per-target linking outcome."""

    reference: dict[str, Any]
    links: dict[str, ReferenceLink]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkedReference:
        return cls(
            reference=data["reference"],
            links={
                name: ReferenceLink.from_dict(link)
                for name, link in (data.get("links") or {}).items()
            },
        )

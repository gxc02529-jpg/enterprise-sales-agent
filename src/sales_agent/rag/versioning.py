from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class VersionDecision(StrEnum):
    PROCEED = "proceed"
    UNCHANGED = "unchanged"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    BUSY = "busy"


class DocumentVersionConflictError(ValueError):
    pass


class DocumentVersionBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionHead:
    active_version: str | None
    active_content_hash: str | None
    active_source_updated_at: datetime | None
    pending_job_id: str | None
    pending_source_updated_at: datetime | None


def decide_version(
    head: VersionHead | None,
    *,
    job_id: str,
    version: str,
    content_hash: str,
    source_updated_at: datetime,
) -> VersionDecision:
    """Make the version decision while the document head row is locked."""

    if head is None:
        return VersionDecision.PROCEED
    if head.active_version == version:
        if head.active_content_hash == content_hash:
            return VersionDecision.UNCHANGED
        return VersionDecision.CONFLICT
    if (
        head.active_source_updated_at is not None
        and source_updated_at <= head.active_source_updated_at
    ):
        return VersionDecision.SUPERSEDED
    if head.pending_job_id and head.pending_job_id != job_id:
        if (
            head.pending_source_updated_at is not None
            and source_updated_at <= head.pending_source_updated_at
        ):
            return VersionDecision.SUPERSEDED
        return VersionDecision.BUSY
    return VersionDecision.PROCEED

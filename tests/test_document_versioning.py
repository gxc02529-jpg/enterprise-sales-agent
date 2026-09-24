from datetime import UTC, datetime, timedelta

from sales_agent.rag.versioning import VersionDecision, VersionHead, decide_version

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def _head(**changes) -> VersionHead:
    values = {
        "active_version": "2",
        "active_content_hash": "hash-2",
        "active_source_updated_at": NOW,
        "pending_job_id": None,
        "pending_source_updated_at": None,
    }
    values.update(changes)
    return VersionHead(**values)


def test_same_version_and_hash_is_idempotent() -> None:
    decision = decide_version(
        _head(),
        job_id="job-3",
        version="2",
        content_hash="hash-2",
        source_updated_at=NOW + timedelta(minutes=1),
    )
    assert decision == VersionDecision.UNCHANGED


def test_same_version_with_different_content_is_conflict() -> None:
    decision = decide_version(
        _head(),
        job_id="job-3",
        version="2",
        content_hash="different",
        source_updated_at=NOW + timedelta(minutes=1),
    )
    assert decision == VersionDecision.CONFLICT


def test_older_source_event_is_superseded_even_if_version_label_differs() -> None:
    decision = decide_version(
        _head(),
        job_id="job-old",
        version="legacy-label",
        content_hash="old",
        source_updated_at=NOW - timedelta(seconds=1),
    )
    assert decision == VersionDecision.SUPERSEDED


def test_newer_event_waits_while_another_generation_is_pending() -> None:
    decision = decide_version(
        _head(
            pending_job_id="job-pending",
            pending_source_updated_at=NOW + timedelta(minutes=1),
        ),
        job_id="job-new",
        version="4",
        content_hash="hash-4",
        source_updated_at=NOW + timedelta(minutes=2),
    )
    assert decision == VersionDecision.BUSY


def test_older_event_loses_to_pending_generation() -> None:
    decision = decide_version(
        _head(
            pending_job_id="job-pending",
            pending_source_updated_at=NOW + timedelta(minutes=2),
        ),
        job_id="job-old",
        version="3",
        content_hash="hash-3",
        source_updated_at=NOW + timedelta(minutes=1),
    )
    assert decision == VersionDecision.SUPERSEDED

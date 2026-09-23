import pytest

from sales_agent.config import Settings
from sales_agent.contracts import (
    DocumentIngestionJob,
    DocumentIngestResponse,
    DocumentJobMetadata,
    Principal,
)
from sales_agent.rag.ingestion import IngestionEnvelope, IngestionQueueFullError
from sales_agent.rag.ingestion_durable import (
    RedisStreamIngestionCoordinator,
    RetrySnapshot,
)
from sales_agent.rag.ingestion_factory import build_ingestion_coordinator
from sales_agent.tools.mock import MockToolGateway


def _principal() -> Principal:
    return Principal(
        user_id="admin-1",
        tenant_id="tenant-1",
        roles=["admin"],
        scope_tags=["region:east"],
    )


def _metadata() -> DocumentJobMetadata:
    return DocumentJobMetadata(
        document_id="document-1",
        title="拜访纪要",
        document_type="visit_note",
        permission_tags=["region:east"],
    )


class _FakeStore:
    def __init__(self) -> None:
        self.created: list[tuple[DocumentIngestionJob, bytes]] = []
        self.deleted: list[str] = []
        self.completed: DocumentIngestResponse | None = None
        self.failed: list[tuple[str, bool]] = []
        self.envelope: IngestionEnvelope | None = None
        self.failure_outcome = "failed"
        self.restored: list[tuple[str, RetrySnapshot]] = []

    async def create(self, job, principal, metadata, content) -> None:
        self.created.append((job, content))

    async def delete(self, job_id: str) -> None:
        self.deleted.append(job_id)

    async def get(self, principal, job_id):
        return self.created[0][0]

    async def list_jobs(self, principal, *, status, limit):
        return [item[0] for item in self.created[:limit]]

    async def retry(self, principal, job_id):
        job = self.created[0][0].model_copy(update={"status": "queued"})
        return job, RetrySnapshot("failed", 3, "TimeoutError", None)

    async def restore_retry(self, job_id, previous) -> None:
        self.restored.append((job_id, previous))

    async def claim(self, job_id, worker_id, stale_after_seconds):
        return self.envelope

    async def mark_indexing(self, job_id, worker_id) -> None:
        return None

    async def complete(self, job_id, worker_id, result) -> None:
        self.completed = result

    async def fail_or_retry(
        self, job_id, worker_id, error_code, *, retryable, max_attempts
    ) -> bool:
        self.failed.append((error_code, retryable))
        return self.failure_outcome

    async def queued_job_ids(self, limit: int) -> list[str]:
        return []

    async def is_terminal(self, job_id: str) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def cleanup(self, payload_retention_days, job_retention_days):
        return {"payloads_cleared": 0, "jobs_deleted": 0}


class _FakeRedis:
    def __init__(self, eval_result=b"1-0", eval_error: Exception | None = None) -> None:
        self.eval_result = eval_result
        self.eval_error = eval_error
        self.added: list[dict[str, str]] = []
        self.added_streams: list[str] = []
        self.acked: list[object] = []
        self.deleted: list[object] = []
        self.released: list[str] = []
        self.eval_calls: list[tuple[object, ...]] = []

    async def eval(self, *args):
        self.eval_calls.append(args)
        if "XACK" in args[0]:
            self.acked.append(args[5])
            self.deleted.append(args[5])
            if int(args[7]) == 1 and args[6]:
                self.released.append(args[6])
            return 1
        if self.eval_error:
            raise self.eval_error
        return self.eval_result

    async def xadd(self, stream, fields):
        self.added_streams.append(stream)
        self.added.append(fields)
        return b"2-0"

    async def xack(self, stream, group, message_id):
        self.acked.append(message_id)

    async def xdel(self, stream, message_id):
        self.deleted.append(message_id)

    async def srem(self, key, job_id):
        self.released.append(job_id)

    async def aclose(self) -> None:
        return None


def _coordinator(store: _FakeStore, redis: _FakeRedis, gateway=None):
    return RedisStreamIngestionCoordinator(
        gateway or MockToolGateway(),
        redis_url="redis://unused",
        stream_name="ingestion",
        consumer_group="indexers",
        max_file_bytes=10_000,
        queue_capacity=10,
        max_attempts=3,
        claim_idle_seconds=300,
        poll_block_ms=100,
        cleanup_interval_seconds=3_600,
        failed_payload_retention_days=7,
        job_retention_days=90,
        store=store,
        client=redis,
        consumer_name="worker-1",
    )


@pytest.mark.asyncio
async def test_durable_submit_persists_before_enqueue() -> None:
    store = _FakeStore()
    redis = _FakeRedis()
    coordinator = _coordinator(store, redis)

    job = await coordinator.submit(
        _principal(),
        _metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content="客户预算已确认".encode(),
    )

    assert job.status == "queued"
    assert store.created[0][0].job_id == job.job_id
    assert store.created[0][1] == "客户预算已确认".encode()


@pytest.mark.asyncio
async def test_durable_submit_removes_row_when_stream_is_full() -> None:
    store = _FakeStore()
    coordinator = _coordinator(store, _FakeRedis(eval_result=None))

    with pytest.raises(IngestionQueueFullError):
        await coordinator.submit(
            _principal(),
            _metadata(),
            filename="visit.md",
            media_type="text/markdown",
            content=b"content",
        )

    assert store.deleted == [store.created[0][0].job_id]


@pytest.mark.asyncio
async def test_broker_outage_keeps_durable_queued_job() -> None:
    store = _FakeStore()
    coordinator = _coordinator(store, _FakeRedis(eval_error=ConnectionError()))

    job = await coordinator.submit(
        _principal(),
        _metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content=b"content",
    )

    assert job.status == "queued"
    assert store.deleted == []


@pytest.mark.asyncio
async def test_worker_indexes_and_acknowledges_message() -> None:
    store = _FakeStore()
    redis = _FakeRedis()
    store.envelope = IngestionEnvelope(
        job_id="job-1",
        principal=_principal(),
        metadata=_metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content="客户第四季度采购。".encode(),
    )
    coordinator = _coordinator(store, redis)

    await coordinator._process_message(b"1-0", {b"job_id": b"job-1"})

    assert store.completed is not None
    assert store.completed.chunk_count == 1
    assert redis.acked == [b"1-0"]
    assert redis.deleted == [b"1-0"]
    assert redis.released == ["job-1"]


class _FailingGateway(MockToolGateway):
    async def ingest_document(self, principal, document, request_id):
        raise TimeoutError("downstream timeout")


@pytest.mark.asyncio
async def test_transient_worker_failure_is_requeued_then_acknowledged() -> None:
    store = _FakeStore()
    store.failure_outcome = "queued"
    store.envelope = IngestionEnvelope(
        job_id="job-2",
        principal=_principal(),
        metadata=_metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content=b"retry me",
    )
    redis = _FakeRedis()
    coordinator = _coordinator(store, redis, _FailingGateway())

    await coordinator._process_message(b"1-0", {b"job_id": b"job-2"})

    assert store.failed == [("TimeoutError", True)]
    assert redis.added == [{"job_id": "job-2"}]
    assert redis.acked == [b"1-0"]
    assert redis.released == []


@pytest.mark.asyncio
async def test_parse_failure_is_not_retried_and_releases_capacity() -> None:
    store = _FakeStore()
    store.envelope = IngestionEnvelope(
        job_id="job-3",
        principal=_principal(),
        metadata=_metadata(),
        filename="unsupported.zip",
        media_type="application/zip",
        content=b"not a supported document",
    )
    redis = _FakeRedis()
    coordinator = _coordinator(store, redis)

    await coordinator._process_message(b"3-0", {b"job_id": b"job-3"})

    assert store.failed == [("DOCUMENT_TYPE_UNSUPPORTED", False)]
    assert redis.added == []
    assert redis.released == ["job-3"]


@pytest.mark.asyncio
async def test_exhausted_transient_failure_goes_to_dead_letter_stream() -> None:
    store = _FakeStore()
    store.failure_outcome = "dead_letter"
    store.envelope = IngestionEnvelope(
        job_id="job-4",
        principal=_principal(),
        metadata=_metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content=b"dead letter me",
    )
    redis = _FakeRedis()
    coordinator = _coordinator(store, redis, _FailingGateway())

    await coordinator._process_message(b"4-0", {b"job_id": b"job-4"})

    assert redis.added_streams == ["ingestion:dead-letter"]
    assert redis.added == [{"job_id": "job-4", "error_code": "TimeoutError"}]
    assert redis.released == ["job-4"]


@pytest.mark.asyncio
async def test_manual_retry_resets_stale_membership_before_enqueue() -> None:
    store = _FakeStore()
    redis = _FakeRedis()
    coordinator = _coordinator(store, redis)
    submitted = await coordinator.submit(
        _principal(),
        _metadata(),
        filename="visit.md",
        media_type="text/markdown",
        content=b"retry payload",
    )

    retried = await coordinator.retry(_principal(), submitted.job_id)

    assert retried.status == "queued"
    assert redis.eval_calls[-1][-1] == 1


@pytest.mark.asyncio
async def test_manual_retry_restores_failed_state_when_queue_is_full() -> None:
    store = _FakeStore()
    redis = _FakeRedis(eval_result=None)
    coordinator = _coordinator(store, redis)
    original = DocumentIngestionJob(
        job_id="5ad25618-8077-4dab-aa19-56c453df4d94",
        tenant_id="tenant-1",
        document_id="document-1",
        filename="visit.md",
        media_type="text/markdown",
        status="failed",
    )
    store.created.append((original, b"retry payload"))

    with pytest.raises(IngestionQueueFullError):
        await coordinator.retry(_principal(), original.job_id)

    assert store.restored[0][0] == original.job_id
    assert store.restored[0][1].status == "failed"
    assert store.restored[0][1].attempt_count == 3


def test_factory_selects_durable_backend() -> None:
    store = _FakeStore()
    redis = _FakeRedis()
    coordinator = build_ingestion_coordinator(
        MockToolGateway(),
        Settings(ingestion_backend="redis_stream"),
        store=store,
        redis_client=redis,
    )
    assert isinstance(coordinator, RedisStreamIngestionCoordinator)

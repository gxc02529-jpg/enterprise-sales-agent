from __future__ import annotations

import asyncio
import json
import os
import socket
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from sales_agent.contracts import (
    DocumentIngestionJob,
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentJobMetadata,
    Principal,
)
from sales_agent.logging import logger
from sales_agent.rag.ingestion import (
    DocumentTooLargeError,
    IngestionEnvelope,
    IngestionQueueFullError,
)
from sales_agent.rag.parsers import DocumentParseError, parse_document
from sales_agent.tools.gateway import ToolGateway


def _driver_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


@dataclass(frozen=True)
class RetrySnapshot:
    status: str
    attempt_count: int
    error_code: str | None
    completed_at: datetime | None


class PostgresIngestionJobStore:
    """Durable source of truth for ingestion payloads, leases and job state."""

    def __init__(
        self,
        database_url: str,
        *,
        pool: Any | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        command_timeout_seconds: int = 15,
    ) -> None:
        self.database_url = database_url
        self._pool = pool
        self._pool_lock = asyncio.Lock()
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self.command_timeout_seconds = command_timeout_seconds

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                try:
                    import asyncpg
                except ImportError as exc:
                    raise RuntimeError(
                        "Durable ingestion requires: pip install '.[infra]'"
                    ) from exc
                self._pool = await asyncpg.create_pool(
                    dsn=_driver_dsn(self.database_url),
                    min_size=self.min_pool_size,
                    max_size=self.max_pool_size,
                    command_timeout=self.command_timeout_seconds,
                )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None and hasattr(self._pool, "close"):
            await self._pool.close()

    async def health(self) -> dict[str, str]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            table = await connection.fetchval(
                "SELECT to_regclass('public.document_ingestion_job')::text"
            )
        if table is None:
            raise RuntimeError("document_ingestion_job table is missing")
        return {"status": "ok", "backend": "postgres"}

    @staticmethod
    async def _set_principal(connection: Any, principal: Principal) -> None:
        await connection.execute(
            "SELECT set_config('app.tenant_id', $1, true), "
            "set_config('app.user_id', $2, true), "
            "set_config('app.is_admin', $3, true), "
            "set_config('app.ingestion_worker', 'false', true)",
            principal.tenant_id,
            principal.user_id,
            str("admin" in principal.roles or "knowledge_admin" in principal.roles).lower(),
        )

    @staticmethod
    async def _set_worker(connection: Any) -> None:
        await connection.execute(
            "SELECT set_config('app.ingestion_worker', 'true', true)"
        )

    @staticmethod
    def _job(row: Any) -> DocumentIngestionJob:
        result = _json_value(row["result"])
        return DocumentIngestionJob(
            job_id=str(row["job_id"]),
            tenant_id=row["tenant_id"],
            document_id=row["document_id"],
            filename=row["filename"],
            media_type=row["media_type"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attempt_count=row["attempt_count"],
            error_code=row["error_code"],
            result=(DocumentIngestResponse.model_validate(result) if result else None),
        )

    @staticmethod
    def _envelope(row: Any) -> IngestionEnvelope:
        metadata = DocumentJobMetadata.model_validate(_json_value(row["metadata"]))
        principal = Principal(
            user_id=row["submitted_by"],
            tenant_id=row["tenant_id"],
            roles=list(row["submitter_roles"] or []),
            scope_tags=list(row["scope_tags"] or []),
        )
        return IngestionEnvelope(
            job_id=str(row["job_id"]),
            principal=principal,
            metadata=metadata,
            filename=row["filename"],
            media_type=row["media_type"],
            content=bytes(row["content"]),
        )

    async def create(
        self,
        job: DocumentIngestionJob,
        principal: Principal,
        metadata: DocumentJobMetadata,
        content: bytes,
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_principal(connection, principal)
            await connection.execute(
                """
                INSERT INTO document_ingestion_job (
                    job_id, tenant_id, document_id, submitted_by,
                    submitter_roles, scope_tags, filename, media_type,
                    metadata, content, status, created_at, updated_at
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5, $6, $7, $8,
                    $9::jsonb, $10, 'queued', $11, $11
                )
                """,
                job.job_id,
                principal.tenant_id,
                metadata.document_id,
                principal.user_id,
                principal.roles,
                principal.scope_tags,
                job.filename,
                job.media_type,
                metadata.model_dump_json(),
                content,
                job.created_at,
            )

    async def delete(self, job_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            await connection.execute(
                "DELETE FROM document_ingestion_job WHERE job_id = $1::uuid", job_id
            )

    async def get(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_principal(connection, principal)
            row = await connection.fetchrow(
                """
                SELECT job_id, tenant_id, document_id, filename, media_type,
                       status, created_at, updated_at, attempt_count,
                       error_code, result
                FROM document_ingestion_job
                WHERE job_id = $1::uuid AND tenant_id = $2
                """,
                job_id,
                principal.tenant_id,
            )
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    async def list_jobs(
        self, principal: Principal, *, status: str | None, limit: int
    ) -> list[DocumentIngestionJob]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_principal(connection, principal)
            rows = await connection.fetch(
                """
                SELECT job_id, tenant_id, document_id, filename, media_type,
                       status, created_at, updated_at, attempt_count,
                       error_code, result
                FROM document_ingestion_job
                WHERE tenant_id = $1 AND ($2::text IS NULL OR status = $2)
                ORDER BY created_at DESC, job_id DESC
                LIMIT $3
                """,
                principal.tenant_id,
                status,
                limit,
            )
        return [self._job(row) for row in rows]

    async def retry(
        self, principal: Principal, job_id: str
    ) -> tuple[DocumentIngestionJob, RetrySnapshot]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_principal(connection, principal)
            current = await connection.fetchrow(
                """
                SELECT status, content, attempt_count, error_code, completed_at
                FROM document_ingestion_job
                WHERE job_id = $1::uuid AND tenant_id = $2
                FOR UPDATE
                """,
                job_id,
                principal.tenant_id,
            )
            if (
                current is None
                or current["status"] not in {"failed", "dead_letter"}
                or current["content"] is None
            ):
                raise KeyError(job_id)
            previous = RetrySnapshot(
                status=current["status"],
                attempt_count=current["attempt_count"],
                error_code=current["error_code"],
                completed_at=current["completed_at"],
            )
            row = await connection.fetchrow(
                """
                UPDATE document_ingestion_job
                SET status = 'queued', attempt_count = 0, error_code = NULL,
                    result = NULL, completed_at = NULL, updated_at = now()
                WHERE job_id = $1::uuid AND tenant_id = $2
                RETURNING job_id, tenant_id, document_id, filename, media_type,
                          status, created_at, updated_at, attempt_count,
                          error_code, result
                """,
                job_id,
                principal.tenant_id,
            )
        return self._job(row), previous

    async def restore_retry(self, job_id: str, previous: RetrySnapshot) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            await connection.execute(
                """
                UPDATE document_ingestion_job
                SET status = $2, attempt_count = $3, error_code = $4,
                    completed_at = $5, updated_at = now()
                WHERE job_id = $1::uuid AND status = 'queued'
                """,
                job_id,
                previous.status,
                previous.attempt_count,
                previous.error_code,
                previous.completed_at,
            )

    async def claim(
        self, job_id: str, worker_id: str, stale_after_seconds: int
    ) -> IngestionEnvelope | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            row = await connection.fetchrow(
                """
                UPDATE document_ingestion_job
                SET status = 'parsing', attempt_count = attempt_count + 1,
                    locked_by = $2, locked_at = now(), updated_at = now(),
                    error_code = NULL
                WHERE job_id = $1::uuid
                  AND content IS NOT NULL
                  AND (
                    status = 'queued'
                    OR (
                      status IN ('parsing', 'indexing')
                      AND locked_at < now() - ($3 * interval '1 second')
                    )
                  )
                RETURNING job_id, tenant_id, submitted_by, submitter_roles,
                          scope_tags, filename, media_type, metadata, content
                """,
                job_id,
                worker_id,
                stale_after_seconds,
            )
        return self._envelope(row) if row is not None else None

    async def mark_indexing(self, job_id: str, worker_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            await connection.execute(
                """
                UPDATE document_ingestion_job
                SET status = 'indexing', locked_at = now(), updated_at = now()
                WHERE job_id = $1::uuid AND locked_by = $2 AND status = 'parsing'
                """,
                job_id,
                worker_id,
            )

    async def complete(
        self, job_id: str, worker_id: str, result: DocumentIngestResponse
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            await connection.execute(
                """
                UPDATE document_ingestion_job
                SET status = 'completed', result = $3::jsonb, content = NULL,
                    locked_by = NULL, locked_at = NULL, completed_at = now(),
                    updated_at = now(), error_code = NULL
                WHERE job_id = $1::uuid AND locked_by = $2
                """,
                job_id,
                worker_id,
                result.model_dump_json(),
            )

    async def fail_or_retry(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        *,
        retryable: bool,
        max_attempts: int,
    ) -> Literal["queued", "failed", "dead_letter"]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            row = await connection.fetchrow(
                """
                UPDATE document_ingestion_job
                SET status = CASE
                      WHEN $4 AND attempt_count < $5 THEN 'queued'
                      WHEN $4 THEN 'dead_letter'
                      ELSE 'failed'
                    END,
                    error_code = $3, locked_by = NULL, locked_at = NULL,
                    completed_at = CASE
                      WHEN $4 AND attempt_count < $5 THEN NULL ELSE now()
                    END,
                    updated_at = now()
                WHERE job_id = $1::uuid AND locked_by = $2
                RETURNING status
                """,
                job_id,
                worker_id,
                error_code[:128],
                retryable,
                max_attempts,
            )
        if row is None:
            return "failed"
        return row["status"]

    async def queued_job_ids(self, limit: int) -> list[str]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            rows = await connection.fetch(
                """
                SELECT job_id
                FROM document_ingestion_job
                WHERE status = 'queued'
                ORDER BY created_at
                LIMIT $1
                """,
                limit,
            )
        return [str(row["job_id"]) for row in rows]

    async def is_terminal(self, job_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            status = await connection.fetchval(
                "SELECT status FROM document_ingestion_job WHERE job_id = $1::uuid",
                job_id,
            )
        return status is None or status in {"completed", "failed", "dead_letter"}

    async def cleanup(self, payload_retention_days: int, job_retention_days: int) -> dict[str, int]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_worker(connection)
            cleared = await connection.fetchval(
                """
                WITH cleared AS (
                    UPDATE document_ingestion_job
                    SET content = NULL, updated_at = now()
                    WHERE status IN ('failed', 'dead_letter')
                      AND content IS NOT NULL
                      AND completed_at < now() - ($1 * interval '1 day')
                    RETURNING 1
                ) SELECT count(*) FROM cleared
                """,
                payload_retention_days,
            )
            deleted = await connection.fetchval(
                """
                WITH deleted AS (
                    DELETE FROM document_ingestion_job
                    WHERE status IN ('completed', 'failed', 'dead_letter')
                      AND completed_at < now() - ($1 * interval '1 day')
                    RETURNING 1
                ) SELECT count(*) FROM deleted
                """,
                job_retention_days,
            )
        return {"payloads_cleared": int(cleared), "jobs_deleted": int(deleted)}


_ENQUEUE_SCRIPT = """
local capacity = tonumber(ARGV[1])
local job_id = ARGV[2]
local reset = ARGV[3] == '1'
if redis.call('SISMEMBER', KEYS[2], job_id) == 1 then
  if not reset then return 'EXISTS' end
  redis.call('SREM', KEYS[2], job_id)
end
if redis.call('SCARD', KEYS[2]) >= capacity then return false end
redis.call('SADD', KEYS[2], job_id)
return redis.call('XADD', KEYS[1], '*', 'job_id', job_id)
"""

_ACK_DELETE_SCRIPT = """
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
if ARGV[4] == '1' and ARGV[3] ~= '' then
  redis.call('SREM', KEYS[2], ARGV[3])
end
return 1
"""


class RedisStreamIngestionCoordinator:
    """At-least-once ingestion using PostgreSQL state and a Redis consumer group."""

    def __init__(
        self,
        gateway: ToolGateway,
        *,
        redis_url: str,
        stream_name: str,
        consumer_group: str,
        max_file_bytes: int,
        queue_capacity: int,
        max_attempts: int,
        claim_idle_seconds: int,
        poll_block_ms: int,
        cleanup_interval_seconds: int,
        failed_payload_retention_days: int,
        job_retention_days: int,
        store: PostgresIngestionJobStore,
        client: Any | None = None,
        consumer_name: str | None = None,
    ) -> None:
        if client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:
                raise RuntimeError(
                    "Redis Stream ingestion requires: pip install '.[infra]'"
                ) from exc
            client = Redis.from_url(redis_url, decode_responses=False)
        self.gateway = gateway
        self.client = client
        self.store = store
        self.stream_name = stream_name
        self.active_set_name = f"{stream_name}:active"
        self.dead_letter_stream_name = f"{stream_name}:dead-letter"
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        )
        self.max_file_bytes = max_file_bytes
        self.queue_capacity = queue_capacity
        self.max_attempts = max_attempts
        self.claim_idle_seconds = claim_idle_seconds
        self.poll_block_ms = poll_block_ms
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.failed_payload_retention_days = failed_payload_retention_days
        self.job_retention_days = job_retention_days
        self._worker: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._processed = 0
        self._failed = 0
        self._dead_lettered = 0
        self._cleaned_payloads = 0
        self._deleted_jobs = 0

    async def start(self) -> None:
        if self._worker is not None:
            return
        try:
            await self.client.xgroup_create(
                self.stream_name, self.consumer_group, id="0-0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        await self._reconcile_queued()
        self._stopping.clear()
        self._worker = asyncio.create_task(
            self._run(), name=f"document-ingestion-{self.consumer_name}"
        )

    async def close(self) -> None:
        self._stopping.set()
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        await self.store.close()
        await self.client.aclose()

    async def submit(
        self,
        principal: Principal,
        metadata: DocumentJobMetadata,
        *,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> DocumentIngestionJob:
        if len(content) > self.max_file_bytes:
            raise DocumentTooLargeError("uploaded document exceeds configured size limit")
        if not content:
            raise DocumentParseError("DOCUMENT_EMPTY")
        now = datetime.now(UTC)
        job = DocumentIngestionJob(
            job_id=str(uuid4()),
            tenant_id=principal.tenant_id,
            document_id=metadata.document_id,
            filename=filename,
            media_type=media_type,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        await self.store.create(job, principal, metadata, content)
        try:
            queued = await self._enqueue(job.job_id)
        except Exception as exc:
            # The durable queued row is intentionally retained. Reconciliation will
            # publish it after Redis recovers, so a transient broker outage loses no job.
            logger.error(
                "document_ingestion_enqueue_deferred",
                job_id=job.job_id,
                error_type=type(exc).__name__,
            )
            return job
        if not queued:
            await self.store.delete(job.job_id)
            raise IngestionQueueFullError("document ingestion queue is full")
        return job

    async def get(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        return await self.store.get(principal, job_id)

    async def list_jobs(
        self, principal: Principal, *, status: str | None, limit: int
    ) -> list[DocumentIngestionJob]:
        return await self.store.list_jobs(principal, status=status, limit=limit)

    async def retry(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        job, previous = await self.store.retry(principal, job_id)
        try:
            queued = await self._enqueue(job_id, reset=True)
        except Exception as exc:
            logger.error(
                "document_ingestion_retry_enqueue_deferred",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            return job
        if not queued:
            await self.store.restore_retry(job_id, previous)
            raise IngestionQueueFullError("document ingestion queue is full")
        return job

    async def health(self) -> dict[str, Any]:
        redis_ok = bool(await self.client.ping())
        store_health = await self.store.health()
        return {
            "status": "ok" if redis_ok and store_health.get("status") == "ok" else "unavailable",
            "backend": "postgres_redis_stream",
            "worker_running": self._worker is not None and not self._worker.done(),
        }

    async def _reconcile_queued(self) -> None:
        for job_id in await self.store.queued_job_ids(self.queue_capacity):
            await self._enqueue(job_id)

    async def _enqueue(self, job_id: str, *, reset: bool = False) -> Any:
        return await self.client.eval(
            _ENQUEUE_SCRIPT,
            2,
            self.stream_name,
            self.active_set_name,
            self.queue_capacity,
            job_id,
            int(reset),
        )

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def _message_job_id(self, fields: dict[Any, Any]) -> str | None:
        raw = fields.get(b"job_id", fields.get("job_id"))
        return self._decode(raw) if raw is not None else None

    async def _ack_delete(
        self, message_id: Any, *, job_id: str | None = None, release: bool = False
    ) -> None:
        await self.client.eval(
            _ACK_DELETE_SCRIPT,
            2,
            self.stream_name,
            self.active_set_name,
            self.consumer_group,
            message_id,
            job_id or "",
            int(release),
        )

    async def _process_message(self, message_id: Any, fields: dict[Any, Any]) -> None:
        job_id = self._message_job_id(fields)
        if not job_id:
            await self._ack_delete(message_id)
            return
        envelope = await self.store.claim(
            job_id, self.consumer_name, self.claim_idle_seconds
        )
        if envelope is None:
            await self._ack_delete(
                message_id,
                job_id=job_id,
                release=await self.store.is_terminal(job_id),
            )
            return
        release_capacity = False
        try:
            text = await parse_document(envelope.filename, envelope.content)
            await self.store.mark_indexing(job_id, self.consumer_name)
            payload = envelope.metadata.model_dump()
            payload["source_uri"] = payload.get("source_uri") or (
                f"upload://{envelope.filename}"
            )
            request = DocumentIngestRequest(**payload, text=text)
            result = await self.gateway.ingest_document(
                envelope.principal, request, envelope.job_id
            )
            await self.store.complete(job_id, self.consumer_name, result)
            self._processed += 1
            release_capacity = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = not isinstance(exc, DocumentParseError)
            error_code = (
                str(exc) if isinstance(exc, DocumentParseError) else type(exc).__name__
            )
            outcome = await self.store.fail_or_retry(
                job_id,
                self.consumer_name,
                error_code,
                retryable=retryable,
                max_attempts=self.max_attempts,
            )
            retry = outcome == "queued"
            if retry:
                await self.client.xadd(self.stream_name, {"job_id": job_id})
            else:
                self._failed += 1
                release_capacity = True
                if outcome == "dead_letter":
                    await self.client.xadd(
                        self.dead_letter_stream_name,
                        {"job_id": job_id, "error_code": error_code[:128]},
                    )
                    self._dead_lettered += 1
            logger.error(
                "document_ingestion_failed",
                job_id=job_id,
                error_type=type(exc).__name__,
                retry_scheduled=retry,
            )
        await self._ack_delete(
            message_id, job_id=job_id, release=release_capacity
        )

    async def _claim_stale(self) -> list[tuple[Any, dict[Any, Any]]]:
        response = await self.client.xautoclaim(
            self.stream_name,
            self.consumer_group,
            self.consumer_name,
            self.claim_idle_seconds * 1000,
            start_id="0-0",
            count=10,
        )
        if not response or len(response) < 2:
            return []
        return list(response[1])

    async def _read_new(self) -> list[tuple[Any, dict[Any, Any]]]:
        response = await self.client.xreadgroup(
            self.consumer_group,
            self.consumer_name,
            {self.stream_name: ">"},
            count=10,
            block=self.poll_block_ms,
        )
        if not response:
            return []
        return [message for _, messages in response for message in messages]

    async def _run(self) -> None:
        last_reconcile = monotonic()
        last_cleanup = monotonic()
        while not self._stopping.is_set():
            try:
                messages = await self._claim_stale()
                if not messages:
                    messages = await self._read_new()
                for message_id, fields in messages:
                    await self._process_message(message_id, fields)
                if monotonic() - last_reconcile >= 30:
                    await self._reconcile_queued()
                    last_reconcile = monotonic()
                if monotonic() - last_cleanup >= self.cleanup_interval_seconds:
                    cleaned = await self.store.cleanup(
                        self.failed_payload_retention_days,
                        self.job_retention_days,
                    )
                    self._cleaned_payloads += cleaned["payloads_cleared"]
                    self._deleted_jobs += cleaned["jobs_deleted"]
                    last_cleanup = monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "document_ingestion_worker_error", error_type=type(exc).__name__
                )
                await asyncio.sleep(1)

    def snapshot(self) -> dict[str, int | str | bool]:
        return {
            "backend": "postgres_redis_stream",
            "stream": self.stream_name,
            "consumer_group": self.consumer_group,
            "worker_running": self._worker is not None and not self._worker.done(),
            "processed": self._processed,
            "failed": self._failed,
            "dead_lettered": self._dead_lettered,
            "payloads_cleared": self._cleaned_payloads,
            "jobs_deleted": self._deleted_jobs,
            "capacity": self.queue_capacity,
        }

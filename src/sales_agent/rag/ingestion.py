from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sales_agent.contracts import (
    DocumentIngestionJob,
    DocumentIngestRequest,
    DocumentJobMetadata,
    Principal,
)
from sales_agent.logging import logger
from sales_agent.rag.parsers import DocumentParseError, parse_document
from sales_agent.tools.gateway import ToolGateway


class DocumentTooLargeError(ValueError):
    pass


class IngestionQueueFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestionEnvelope:
    job_id: str
    principal: Principal
    metadata: DocumentJobMetadata
    filename: str
    media_type: str
    content: bytes


class IngestionCoordinator(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def submit(
        self,
        principal: Principal,
        metadata: DocumentJobMetadata,
        *,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> DocumentIngestionJob: ...

    async def get(self, principal: Principal, job_id: str) -> DocumentIngestionJob: ...

    async def list_jobs(
        self, principal: Principal, *, status: str | None, limit: int
    ) -> list[DocumentIngestionJob]: ...

    async def retry(self, principal: Principal, job_id: str) -> DocumentIngestionJob: ...

    async def health(self) -> dict[str, Any]: ...

    def snapshot(self) -> dict[str, Any]: ...


class InMemoryIngestionCoordinator:
    """Bounded development worker; the store/queue boundary is replaceable by PostgreSQL."""

    def __init__(self, gateway: ToolGateway, *, max_file_bytes: int, queue_capacity: int) -> None:
        self.gateway = gateway
        self.max_file_bytes = max_file_bytes
        self.queue: asyncio.Queue[IngestionEnvelope | None] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self.jobs: dict[str, DocumentIngestionJob] = {}
        self.envelopes: dict[str, IngestionEnvelope] = {}
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="document-ingestion-worker")

    async def close(self) -> None:
        if self._worker is None:
            return
        await self.queue.put(None)
        await self._worker
        self._worker = None

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
        if self.queue.full():
            raise IngestionQueueFullError("document ingestion queue is full")
        job_id = str(uuid4())
        now = datetime.now(UTC)
        job = DocumentIngestionJob(
            job_id=job_id,
            tenant_id=principal.tenant_id,
            document_id=metadata.document_id,
            filename=filename,
            media_type=media_type,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self.jobs[job_id] = job
        envelope = IngestionEnvelope(
            job_id, principal, metadata, filename, media_type, content
        )
        self.envelopes[job_id] = envelope
        self.queue.put_nowait(envelope)
        return job

    async def get(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        async with self._lock:
            job = self.jobs.get(job_id)
        if job is None or job.tenant_id != principal.tenant_id:
            raise KeyError(job_id)
        return job

    async def list_jobs(
        self, principal: Principal, *, status: str | None, limit: int
    ) -> list[DocumentIngestionJob]:
        async with self._lock:
            jobs = [
                job
                for job in self.jobs.values()
                if job.tenant_id == principal.tenant_id
                and (status is None or job.status == status)
            ]
        jobs.sort(key=lambda item: (item.created_at, item.job_id), reverse=True)
        return jobs[:limit]

    async def retry(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        if self.queue.full():
            raise IngestionQueueFullError("document ingestion queue is full")
        async with self._lock:
            job = self.jobs.get(job_id)
            envelope = self.envelopes.get(job_id)
            if (
                job is None
                or job.tenant_id != principal.tenant_id
                or job.status not in {"failed", "dead_letter"}
                or envelope is None
            ):
                raise KeyError(job_id)
            retried = job.model_copy(
                update={
                    "status": "queued",
                    "attempt_count": 0,
                    "error_code": None,
                    "updated_at": datetime.now(UTC),
                }
            )
            self.jobs[job_id] = retried
        self.queue.put_nowait(envelope)
        return retried

    async def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": "memory",
            "worker_running": self._worker is not None and not self._worker.done(),
        }

    async def _update(self, job_id: str, **changes: Any) -> None:
        async with self._lock:
            current = self.jobs[job_id]
            self.jobs[job_id] = current.model_copy(
                update={**changes, "updated_at": datetime.now(UTC)}
            )

    async def _process(self, envelope: IngestionEnvelope) -> None:
        current = await self.get(envelope.principal, envelope.job_id)
        await self._update(
            envelope.job_id,
            status="parsing",
            attempt_count=current.attempt_count + 1,
        )
        text = await parse_document(envelope.filename, envelope.content)
        await self._update(envelope.job_id, status="indexing")
        payload = envelope.metadata.model_dump()
        payload["source_uri"] = payload.get("source_uri") or f"upload://{envelope.filename}"
        request = DocumentIngestRequest(**payload, text=text)
        result = await self.gateway.ingest_document(
            envelope.principal, request, envelope.job_id
        )
        await self._update(envelope.job_id, status="completed", result=result)
        self.envelopes.pop(envelope.job_id, None)

    async def _run(self) -> None:
        while True:
            envelope = await self.queue.get()
            try:
                if envelope is None:
                    return
                try:
                    await self._process(envelope)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._update(
                        envelope.job_id,
                        status="failed",
                        error_code=(
                            str(exc)
                            if isinstance(exc, DocumentParseError)
                            else type(exc).__name__
                        ),
                    )
                    logger.error(
                        "document_ingestion_failed",
                        job_id=envelope.job_id,
                        error_type=type(exc).__name__,
                    )
            finally:
                self.queue.task_done()

    def snapshot(self) -> dict[str, int | str]:
        return {
            "backend": "memory",
            "queued": self.queue.qsize(),
            "capacity": self.queue.maxsize,
            "tracked_jobs": len(self.jobs),
        }

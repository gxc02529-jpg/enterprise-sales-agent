from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
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
class _Envelope:
    job_id: str
    principal: Principal
    metadata: DocumentJobMetadata
    filename: str
    media_type: str
    content: bytes


class InMemoryIngestionCoordinator:
    """Bounded development worker; the store/queue boundary is replaceable by PostgreSQL."""

    def __init__(self, gateway: ToolGateway, *, max_file_bytes: int, queue_capacity: int) -> None:
        self.gateway = gateway
        self.max_file_bytes = max_file_bytes
        self.queue: asyncio.Queue[_Envelope | None] = asyncio.Queue(maxsize=queue_capacity)
        self.jobs: dict[str, DocumentIngestionJob] = {}
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
        self.queue.put_nowait(
            _Envelope(job_id, principal, metadata, filename, media_type, content)
        )
        return job

    async def get(self, principal: Principal, job_id: str) -> DocumentIngestionJob:
        async with self._lock:
            job = self.jobs.get(job_id)
        if job is None or job.tenant_id != principal.tenant_id:
            raise KeyError(job_id)
        return job

    async def _update(self, job_id: str, **changes: Any) -> None:
        async with self._lock:
            current = self.jobs[job_id]
            self.jobs[job_id] = current.model_copy(
                update={**changes, "updated_at": datetime.now(UTC)}
            )

    async def _process(self, envelope: _Envelope) -> None:
        await self._update(envelope.job_id, status="parsing")
        text = await parse_document(envelope.filename, envelope.content)
        await self._update(envelope.job_id, status="indexing")
        request = DocumentIngestRequest(
            **envelope.metadata.model_dump(),
            text=text,
            source_uri=envelope.metadata.source_uri or f"upload://{envelope.filename}",
        )
        result = await self.gateway.ingest_document(
            envelope.principal, request, envelope.job_id
        )
        await self._update(envelope.job_id, status="completed", result=result)

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

from __future__ import annotations

import asyncio
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sales_agent.config import Settings
from sales_agent.logging import logger


def payload_digest(payload: Any) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    tenant_id: str
    user_id: str
    request_id: str
    event_type: str
    status: str
    session_id: str | None = None
    tool_name: str | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    elapsed_ms: int | None = None
    token_usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditSink(ABC):
    @abstractmethod
    async def emit(self, event: AuditEvent) -> None: ...


class StructuredLogAuditSink(AuditSink):
    async def emit(self, event: AuditEvent) -> None:
        logger.info("audit_event", **event.__dict__)


class PostgresAuditSink(AuditSink):
    def __init__(
        self,
        database_url: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        command_timeout_seconds: int = 15,
    ) -> None:
        self.database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._pool: Any | None = None
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
                    raise RuntimeError("PostgreSQL audit requires: pip install '.[infra]'") from exc
                self._pool = await asyncpg.create_pool(
                    dsn=self.database_url,
                    min_size=self.min_pool_size,
                    max_size=self.max_pool_size,
                    command_timeout=self.command_timeout_seconds,
                )
        return self._pool

    async def emit(self, event: AuditEvent) -> None:
        pool = await self._get_pool()
        await pool.execute(
            """
            INSERT INTO audit_event (
                tenant_id, user_id, request_id, session_id, event_type,
                tool_name, input_digest, output_digest, status, elapsed_ms,
                token_usage, metadata
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb)
            """,
            event.tenant_id,
            event.user_id,
            event.request_id,
            event.session_id,
            event.event_type,
            event.tool_name,
            event.input_digest,
            event.output_digest,
            event.status,
            event.elapsed_ms,
            json.dumps(event.token_usage, ensure_ascii=False),
            json.dumps(event.metadata, ensure_ascii=False),
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


def build_audit_sink(settings: Settings) -> AuditSink:
    if settings.audit_backend == "postgres":
        return PostgresAuditSink(
            settings.database_url,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
            command_timeout_seconds=settings.postgres_command_timeout_seconds,
        )
    return StructuredLogAuditSink()

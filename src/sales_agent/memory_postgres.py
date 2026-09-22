from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sales_agent.contracts import MemoryRecord, Principal
from sales_agent.memory import MemoryService


def _driver_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresMemoryService(MemoryService):
    """Persistent user/business memory with approval state and RLS context."""

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
                        "PostgreSQL memory requires: pip install '.[infra]'"
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

    @staticmethod
    async def _set_rls_context(
        connection: Any,
        principal: Principal,
        session_id: str = "",
    ) -> None:
        await connection.execute(
            "SELECT set_config('app.tenant_id', $1, true), "
            "set_config('app.user_id', $2, true), "
            "set_config('app.scope_tags', $3, true), "
            "set_config('app.is_admin', $4, true), "
            "set_config('app.session_id', $5, true)",
            principal.tenant_id,
            principal.user_id,
            ",".join(principal.scope_tags),
            str("admin" in principal.roles).lower(),
            session_id,
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower()))

    @staticmethod
    def _record(row: Any) -> MemoryRecord:
        payload = dict(row)
        payload["memory_id"] = str(payload["memory_id"])
        return MemoryRecord.model_validate(payload)

    async def recall(
        self,
        principal: Principal,
        session_id: str,
        query: str,
        token_budget: int,
    ) -> list[MemoryRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_rls_context(connection, principal, session_id)
            records = await connection.fetch(
                """
                SELECT memory_id, layer, content, owner_user_id, tenant_id,
                       permission_tags, entity_ids, status, created_at, expires_at
                FROM memory_record
                WHERE tenant_id = $1
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                  AND (
                    (layer = 'session' AND session_id = $2)
                    OR (layer = 'user' AND owner_user_id = $3)
                    OR layer = 'business'
                  )
                ORDER BY created_at DESC
                LIMIT 200
                """,
                principal.tenant_id,
                session_id,
                principal.user_id,
            )
        query_terms = self._terms(query)
        ranked: list[tuple[int, MemoryRecord]] = []
        for row in records:
            record = self._record(row)
            score = len(query_terms.intersection(self._terms(record.content)))
            ranked.append((score, record))
        ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        selected: list[MemoryRecord] = []
        used = 0
        for _, record in ranked:
            estimated_tokens = max(1, len(record.content) // 2)
            if used + estimated_tokens > token_budget:
                continue
            selected.append(record)
            used += estimated_tokens
        return selected

    async def propose_user_memory(self, principal: Principal, content: str) -> MemoryRecord:
        pool = await self._get_pool()
        memory_id = uuid4()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_rls_context(connection, principal)
            row = await connection.fetchrow(
                """
                INSERT INTO memory_record (
                    memory_id, tenant_id, layer, owner_user_id, content,
                    permission_tags, status, created_at
                ) VALUES ($1, $2, 'user', $3, $4, $5, 'candidate', $6)
                RETURNING memory_id, layer, content, owner_user_id, tenant_id,
                          permission_tags, entity_ids, status, created_at, expires_at
                """,
                memory_id,
                principal.tenant_id,
                principal.user_id,
                content,
                principal.scope_tags,
                datetime.now(UTC),
            )
        return self._record(row)

    async def confirm_user_memory(self, principal: Principal, memory_id: str) -> MemoryRecord:
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_rls_context(connection, principal)
            row = await connection.fetchrow(
                """
                UPDATE memory_record
                SET status = 'active', reviewed_by = $1, reviewed_at = now()
                WHERE memory_id = $2::uuid
                  AND tenant_id = $3
                  AND layer = 'user'
                  AND owner_user_id = $1
                  AND status IN ('candidate', 'active')
                RETURNING memory_id, layer, content, owner_user_id, tenant_id,
                          permission_tags, entity_ids, status, created_at, expires_at
                """,
                principal.user_id,
                memory_id,
                principal.tenant_id,
            )
        if row is None:
            raise KeyError(memory_id)
        return self._record(row)

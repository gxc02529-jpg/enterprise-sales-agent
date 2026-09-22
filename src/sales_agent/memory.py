from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from sales_agent.contracts import MemoryRecord, Principal


class MemoryService(ABC):
    @abstractmethod
    async def recall(
        self, principal: Principal, session_id: str, query: str, token_budget: int
    ) -> list[MemoryRecord]: ...

    @abstractmethod
    async def propose_user_memory(self, principal: Principal, content: str) -> MemoryRecord: ...

    @abstractmethod
    async def confirm_user_memory(self, principal: Principal, memory_id: str) -> MemoryRecord: ...


class InMemoryMemoryService(MemoryService):
    """Development adapter preserving the same approval and isolation invariants as production."""

    def __init__(self) -> None:
        self._session: dict[str, list[MemoryRecord]] = defaultdict(list)
        self._user: dict[str, list[MemoryRecord]] = defaultdict(list)
        self._business: dict[str, list[MemoryRecord]] = defaultdict(list)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower()))

    @staticmethod
    def _authorized(record: MemoryRecord, principal: Principal) -> bool:
        if record.tenant_id != principal.tenant_id:
            return False
        if record.owner_user_id and record.owner_user_id != principal.user_id:
            return False
        permitted = set(record.permission_tags).intersection(principal.scope_tags)
        if record.permission_tags and not permitted:
            return "admin" in principal.roles
        return True

    async def recall(
        self, principal: Principal, session_id: str, query: str, token_budget: int
    ) -> list[MemoryRecord]:
        now = datetime.now(UTC)
        candidates = (
            self._session.get(session_id, [])
            + self._user.get(principal.user_id, [])
            + self._business.get(principal.tenant_id, [])
        )
        query_terms = self._terms(query)
        ranked: list[tuple[int, MemoryRecord]] = []
        for record in candidates:
            if record.status != "active" or not self._authorized(record, principal):
                continue
            if record.expires_at and record.expires_at <= now:
                continue
            overlap = len(query_terms.intersection(self._terms(record.content)))
            ranked.append((overlap, record))
        ranked.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        selected: list[MemoryRecord] = []
        used = 0
        for _, record in ranked:
            estimated = max(1, len(record.content) // 2)
            if used + estimated > token_budget:
                continue
            selected.append(record)
            used += estimated
        return selected

    async def propose_user_memory(self, principal: Principal, content: str) -> MemoryRecord:
        candidate = MemoryRecord(
            memory_id=str(uuid4()),
            layer="user",
            content=content,
            owner_user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            permission_tags=principal.scope_tags,
            status="candidate",
        )
        self._user[principal.user_id].append(candidate)
        return candidate

    async def confirm_user_memory(self, principal: Principal, memory_id: str) -> MemoryRecord:
        for record in self._user.get(principal.user_id, []):
            if record.memory_id == memory_id and record.tenant_id == principal.tenant_id:
                if record.status == "candidate":
                    record.status = "active"
                return record
        raise KeyError(memory_id)

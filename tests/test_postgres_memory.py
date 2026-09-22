from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from sales_agent.contracts import Principal
from sales_agent.memory_postgres import PostgresMemoryService


class AsyncContext:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeMemoryConnection:
    def __init__(self) -> None:
        self.context_args: tuple[Any, ...] = ()

    def transaction(self) -> AsyncContext:
        return AsyncContext()

    async def execute(self, _: str, *args: Any) -> None:
        self.context_args = args

    async def fetch(self, _: str, *args: Any) -> list[dict[str, Any]]:
        assert args == ("tenant-1", "session-1", "sales-1")
        now = datetime.now(UTC)
        return [
            {
                "memory_id": uuid4(),
                "layer": "business",
                "content": "华东智造预算已经增加",
                "owner_user_id": None,
                "tenant_id": "tenant-1",
                "permission_tags": ["region:east"],
                "entity_ids": ["customer-1"],
                "status": "active",
                "created_at": now,
                "expires_at": None,
            },
            {
                "memory_id": uuid4(),
                "layer": "user",
                "content": "用户偏好按季度查看",
                "owner_user_id": "sales-1",
                "tenant_id": "tenant-1",
                "permission_tags": ["sales:sales-1"],
                "entity_ids": [],
                "status": "active",
                "created_at": now,
                "expires_at": None,
            },
        ]


class FakePool:
    def __init__(self, connection: FakeMemoryConnection) -> None:
        self.connection = connection

    def acquire(self) -> AsyncContext:
        return AsyncContext(self.connection)


@pytest.mark.asyncio
async def test_postgres_memory_ranks_after_rls_filter() -> None:
    connection = FakeMemoryConnection()
    service = PostgresMemoryService("postgresql://unused", pool=FakePool(connection))
    principal = Principal(
        user_id="sales-1",
        tenant_id="tenant-1",
        roles=["sales"],
        scope_tags=["sales:sales-1", "region:east"],
    )
    records = await service.recall(principal, "session-1", "华东智造预算", token_budget=100)
    assert records[0].layer == "business"
    assert connection.context_args[-1] == "session-1"

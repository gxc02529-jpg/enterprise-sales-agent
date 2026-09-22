from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from sales_agent.contracts import Principal, SalesMetricParams
from sales_agent.tools.postgres import PostgresSalesGateway, build_sales_metrics_query


class AsyncContext:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: tuple[str, tuple[Any, ...]] | None = None

    def transaction(self) -> AsyncContext:
        return AsyncContext()

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched = (sql, args)
        return [{"quarter": "2026-Q1", "revenue": Decimal("128000.50")}]


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> AsyncContext:
        return AsyncContext(self.connection)


@pytest.fixture
def principal() -> Principal:
    return Principal(
        user_id="sales-1",
        tenant_id="tenant-1",
        roles=["sales"],
        scope_tags=["sales:sales-1", "region:east"],
    )


def test_query_compiler_uses_only_positional_values(principal: Principal) -> None:
    params = SalesMetricParams(
        metrics=["revenue", "contract_count"],
        dimensions=["quarter", "product", "industry"],
        start_date=date(2026, 1, 1),
        end_date=date(2026, 9, 30),
        customer_ids=["customer-1"],
        product_ids=["product-1"],
        limit=50,
    )
    query = build_sales_metrics_query(principal, params)
    assert "JOIN customer c" in query.sql
    assert "JOIN contract_item ci" in query.sql
    assert "customer-1" not in query.sql
    assert "product-1" not in query.sql
    assert query.arguments[-1] == 50
    assert query.sql.endswith("LIMIT $9::integer")


@pytest.mark.asyncio
async def test_gateway_sets_rls_context_and_serializes_decimals(
    principal: Principal,
) -> None:
    connection = FakeConnection()
    gateway = PostgresSalesGateway(
        "postgresql://unused",
        pool=FakePool(connection),
    )
    result = await gateway.sales_metrics(
        principal,
        SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]),
        "request-1",
    )
    _, rls_args = connection.executed[0]
    assert rls_args == (
        "tenant-1",
        "sales-1",
        "sales:sales-1,region:east",
        "false",
    )
    assert result.data == [{"quarter": "2026-Q1", "revenue": 128000.5}]
    assert result.citations[0].source_id.startswith("sql-template:")

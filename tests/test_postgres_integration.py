import os
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set; run against a real PostgreSQL (see CI workflow)",
)

ROOT = Path(__file__).resolve().parents[1]


def _statements(sql: str) -> list[str]:
    out: list[str] = []
    for block in sql.split(";"):
        lines = [line for line in block.splitlines() if not line.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            out.append(stmt)
    return out


async def _setup(conn) -> None:
    ddl = (ROOT / "deploy" / "postgres" / "init.sql").read_text(encoding="utf-8")
    seed = (ROOT / "examples" / "demo_seed.sql").read_text(encoding="utf-8")
    statements = _statements(ddl) + _statements(seed)
    # Drop explicit transaction markers; asyncpg manages transactions itself.
    statements = [
        stmt
        for stmt in statements
        if stmt.upper() not in ("BEGIN", "COMMIT", "START TRANSACTION")
    ]
    async with conn.transaction():
        for stmt in statements:
            await conn.execute(stmt)


@pytest.fixture
async def gateway():
    from sales_agent.tools.postgres import PostgresSalesGateway

    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await _setup(conn)
    finally:
        await conn.close()
    gateway = PostgresSalesGateway(TEST_DATABASE_URL)
    try:
        yield gateway
    finally:
        await gateway.close()


def _principal(user_id: str, roles: list[str] | None = None, scope: list[str] | None = None):
    from sales_agent.contracts import Principal

    return Principal(
        user_id=user_id,
        tenant_id="demo-tenant",
        roles=roles or ["sales"],
        scope_tags=scope or [],
    )


def _revenue_total(result) -> float:
    return sum(row["revenue"] for row in result.data)


@pytest.mark.asyncio
async def test_rls_scopes_sales_to_own_contracts(gateway) -> None:
    from sales_agent.contracts import SalesMetricParams

    east = _principal("demo-sales-001", scope=["sales:demo-sales-001", "region:east"])
    result = await gateway.sales_metrics(
        east, SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]), "r1"
    )
    # 华东销售只能看到华东智造的 3 份合同，看不到华南 HT-2026-044
    assert _revenue_total(result) == 5190000.0


@pytest.mark.asyncio
async def test_rls_isolates_second_sales(gateway) -> None:
    from sales_agent.contracts import SalesMetricParams

    south = _principal("demo-sales-002", scope=["sales:demo-sales-002", "region:south"])
    result = await gateway.sales_metrics(
        south, SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]), "r2"
    )
    assert _revenue_total(result) == 960000.0


@pytest.mark.asyncio
async def test_admin_sees_all_contracts(gateway) -> None:
    from sales_agent.contracts import SalesMetricParams

    admin = _principal("admin-1", roles=["admin"])
    result = await gateway.sales_metrics(
        admin, SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]), "r3"
    )
    assert _revenue_total(result) == 6150000.0

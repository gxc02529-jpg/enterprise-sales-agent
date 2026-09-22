from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

from sales_agent.contracts import (
    Citation,
    Principal,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.tools.mock import MockToolGateway

METRIC_EXPRESSIONS = {
    "revenue": "COALESCE(SUM(sc.amount), 0)::numeric AS revenue",
    "contract_count": "COUNT(DISTINCT sc.contract_id)::bigint AS contract_count",
    "customer_count": "COUNT(DISTINCT sc.customer_id)::bigint AS customer_count",
    "avg_deal_size": "COALESCE(AVG(sc.amount), 0)::numeric AS avg_deal_size",
}

DIMENSION_EXPRESSIONS = {
    "month": "to_char(sc.signed_at, 'YYYY-MM') AS month",
    "quarter": "to_char(date_trunc('quarter', sc.signed_at), 'YYYY-\"Q\"Q') AS quarter",
    "salesperson": "sc.owner_employee_id AS salesperson",
    "customer": "sc.customer_id AS customer",
    "product": "ci.product_id AS product",
    "industry": "c.industry_code AS industry",
}

DIMENSION_GROUPS = {
    "month": "to_char(sc.signed_at, 'YYYY-MM')",
    "quarter": "to_char(date_trunc('quarter', sc.signed_at), 'YYYY-\"Q\"Q')",
    "salesperson": "sc.owner_employee_id",
    "customer": "sc.customer_id",
    "product": "ci.product_id",
    "industry": "c.industry_code",
}


def _driver_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


@dataclass(frozen=True)
class ParameterizedQuery:
    sql: str
    arguments: tuple[Any, ...]
    template_id: str


def build_sales_metrics_query(
    principal: Principal, params: SalesMetricParams
) -> ParameterizedQuery:
    """Compile allow-listed metrics and dimensions; user text never enters SQL."""

    dimensions = list(dict.fromkeys(params.dimensions))
    metrics = list(dict.fromkeys(params.metrics))
    select_parts = [DIMENSION_EXPRESSIONS[item] for item in dimensions]
    select_parts.extend(METRIC_EXPRESSIONS[item] for item in metrics)
    group_parts = [DIMENSION_GROUPS[item] for item in dimensions]

    needs_customer = "industry" in dimensions
    needs_product = "product" in dimensions or bool(params.product_ids)
    joins: list[str] = []
    if needs_customer:
        joins.append("JOIN customer c ON c.customer_id = sc.customer_id")
    if needs_product:
        joins.append("JOIN contract_item ci ON ci.contract_id = sc.contract_id")

    arguments: list[Any] = [
        principal.tenant_id,
        "admin" in principal.roles,
        principal.user_id,
        principal.scope_tags,
    ]
    filters = [
        "sc.tenant_id = $1",
        "($2::boolean OR sc.owner_employee_id = $3 OR sc.permission_tags && $4::text[])",
    ]

    def add_filter(expression: str, value: Any) -> None:
        arguments.append(value)
        filters.append(expression.format(index=len(arguments)))

    if params.start_date:
        add_filter("sc.signed_at >= ${index}::date", params.start_date)
    if params.end_date:
        add_filter("sc.signed_at <= ${index}::date", params.end_date)
    if params.customer_ids:
        add_filter("sc.customer_id = ANY(${index}::text[])", params.customer_ids)
    if params.product_ids:
        add_filter("ci.product_id = ANY(${index}::text[])", params.product_ids)

    arguments.append(params.limit)
    limit_position = len(arguments)
    group_clause = f"GROUP BY {', '.join(group_parts)}" if group_parts else ""
    order_clause = f"ORDER BY {', '.join(group_parts)}" if group_parts else ""
    sql = "\n".join(
        part
        for part in [
            f"SELECT {', '.join(select_parts)}",
            "FROM sales_contract sc",
            *joins,
            f"WHERE {' AND '.join(filters)}",
            group_clause,
            order_clause,
            f"LIMIT ${limit_position}::integer",
        ]
        if part
    )
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
    return ParameterizedQuery(sql=sql, arguments=tuple(arguments), template_id=digest)


class PostgresSalesGateway(MockToolGateway):
    """M1 backend: real PostgreSQL metrics plus explicit development fallbacks."""

    def __init__(
        self,
        database_url: str,
        export_dir: Path | None = None,
        *,
        pool: Any | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        command_timeout_seconds: int = 15,
    ) -> None:
        super().__init__(export_dir)
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
                        "PostgreSQL backend requires: pip install '.[infra]'"
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

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        self._require_scope(principal)
        started = perf_counter()
        query = build_sales_metrics_query(principal, params)
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('app.tenant_id', $1, true), "
                "set_config('app.user_id', $2, true), "
                "set_config('app.scope_tags', $3, true), "
                "set_config('app.is_admin', $4, true)",
                principal.tenant_id,
                principal.user_id,
                ",".join(principal.scope_tags),
                str("admin" in principal.roles).lower(),
            )
            records = await connection.fetch(query.sql, *query.arguments)
        rows = [
            {key: _json_value(value) for key, value in dict(record).items()} for record in records
        ]
        return ToolResult(
            tool_name="sales_metrics",
            route=Route.SQL,
            data=rows,
            row_count=len(rows),
            elapsed_ms=int((perf_counter() - started) * 1000),
            citations=[
                Citation(
                    source_type="sql",
                    source_id=f"sql-template:{query.template_id}:request:{request_id}",
                    label="CRM 销售合同聚合查询",
                    locator=(
                        f"metrics={','.join(params.metrics)};"
                        f"dimensions={','.join(params.dimensions)}"
                    ),
                )
            ],
        )

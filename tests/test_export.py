from pathlib import Path

import pytest

from sales_agent.config import Settings
from sales_agent.contracts import Principal, SalesMetricParams
from sales_agent.tools.export import ExcelExportGateway, _build_rows
from sales_agent.tools.mock import MockToolGateway


def _principal() -> Principal:
    return Principal(
        user_id="sales-1",
        tenant_id="demo-tenant",
        roles=["sales"],
        scope_tags=["region:east"],
    )


def test_build_rows_extracts_rows_and_columns() -> None:
    payload = {
        "query": "统计季度销售额",
        "prior_results": [
            {
                "tool_name": "sales_metrics",
                "route": "sql",
                "data": [
                    {"quarter": "2026-Q1", "revenue": 128000},
                    {"quarter": "2026-Q2", "revenue": 96000},
                ],
            }
        ],
    }
    rows, columns = _build_rows(payload)
    assert len(rows) == 2
    assert "_tool" in columns and "revenue" in columns
    assert rows[0]["_route"] == "sql"


@pytest.mark.asyncio
async def test_export_writes_real_file(tmp_path: Path) -> None:
    gateway = ExcelExportGateway(MockToolGateway(), Settings(export_dir=Path(tmp_path)))
    result = await gateway.export_report(
        _principal(),
        {
            "query": "统计季度销售额",
            "prior_results": [
                {
                    "tool_name": "sales_metrics",
                    "route": "sql",
                    "data": [{"quarter": "2026-Q1", "revenue": 128000}],
                }
            ],
        },
        "request-123",
    )
    assert result.simulated is False
    assert result.data["status"] == "ready"
    filename = result.data["filename"]
    path = Path(tmp_path) / filename
    assert path.exists()
    assert path.stat().st_size > 0
    assert result.data["download_url"] == f"/exports/{filename}"


@pytest.mark.asyncio
async def test_gateway_delegates_sales_to_inner(tmp_path: Path) -> None:
    gateway = ExcelExportGateway(MockToolGateway(), Settings(export_dir=Path(tmp_path)))
    result = await gateway.sales_metrics(
        _principal(),
        SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]),
        "request-1",
    )
    assert result.tool_name == "sales_metrics"
    assert result.simulated is True

from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sales_agent.config import Settings
from sales_agent.contracts import (
    Citation,
    DocumentIngestRequest,
    DocumentIngestResponse,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.tools.gateway import ToolGateway


def _flatten_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _build_rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    for result in payload.get("prior_results") or []:
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        if not isinstance(result, dict):
            continue
        for row in _flatten_rows(result.get("data")):
            rows.append(
                {
                    **row,
                    "_tool": result.get("tool_name", ""),
                    "_route": result.get("route", ""),
                }
            )
    columns = sorted({key for row in rows for key in row})
    return rows, columns


class ExcelExportGateway(ToolGateway):
    """Compose a real file exporter around an inner gateway.

    Only ``export_report`` is overridden; the analysis tools delegate to ``inner``.
    Writes a real file under ``settings.export_dir`` (xlsx via openpyxl when available,
    otherwise CSV as a zero-dependency fallback) so the download URL actually resolves.
    """

    def __init__(self, inner: ToolGateway, settings: Settings) -> None:
        self.inner = inner
        self.settings = settings

    def _result(
        self, path: Path, filename: str, row_count: int, fmt: str
    ) -> ToolResult:
        return ToolResult(
            tool_name="export_report",
            route=Route.EXPORT,
            data={
                "status": "ready",
                "filename": filename,
                "download_url": f"/exports/{filename}",
                "format": fmt,
                "row_count": row_count,
            },
            row_count=row_count,
            confidence=1.0,
            citations=[
                Citation(
                    source_type="export",
                    source_id=f"export:{filename}",
                    label="分析报表导出（真实文件）",
                    excerpt=f"{row_count} 行 / {fmt}",
                )
            ],
        )

    def _export_csv(
        self, export_dir: Path, payload: dict[str, Any], rows: list[dict[str, Any]],
        columns: list[str], request_id: str,
    ) -> ToolResult:
        filename = f"sales-analysis-{request_id}.csv"
        path = export_dir / filename
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return self._result(path, filename, len(rows), "csv")

    def _export_xlsx(
        self, export_dir: Path, payload: dict[str, Any], rows: list[dict[str, Any]],
        columns: list[str], request_id: str,
    ) -> ToolResult:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "analysis"
        sheet.append(["query", "generated_at"])
        sheet.append([payload.get("query", ""), datetime.now(UTC).isoformat()])
        sheet.append([])
        sheet.append(columns)
        for row in rows:
            sheet.append([row.get(col) for col in columns])
        filename = f"sales-analysis-{request_id}.xlsx"
        path = export_dir / filename
        workbook.save(path)
        return self._result(path, filename, len(rows), "xlsx")

    def _export_sync(self, payload: dict[str, Any], request_id: str) -> ToolResult:
        export_dir = Path(self.settings.export_dir).resolve()
        export_dir.mkdir(parents=True, exist_ok=True)
        rows, columns = _build_rows(payload)
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return self._export_csv(export_dir, payload, rows, columns, request_id)
        return self._export_xlsx(export_dir, payload, rows, columns, request_id)

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        return await asyncio.to_thread(self._export_sync, payload, request_id)

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        return await self.inner.sales_metrics(principal, params, request_id)

    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult:
        return await self.inner.graph_relations(principal, params, request_id)

    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult:
        return await self.inner.search_documents(principal, params, request_id)

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        return await self.inner.ingest_document(principal, document, request_id)

    async def health(self) -> dict[str, Any]:
        base = await self.inner.health()
        return {**base, "export_backend": "file"}

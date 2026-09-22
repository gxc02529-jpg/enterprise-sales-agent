from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

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
from sales_agent.rag.chunking import chunk_document
from sales_agent.tools.gateway import ToolGateway


class MockToolGateway(ToolGateway):
    """Deterministic data makes the entire graph runnable before infrastructure is connected."""

    def __init__(self, export_dir: Path | None = None) -> None:
        self.export_dir = export_dir or Path("exports")

    @staticmethod
    def _require_scope(principal: Principal) -> None:
        if not principal.scope_tags and "admin" not in principal.roles:
            raise PermissionError("principal has no permitted data scope")

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        started = perf_counter()
        self._require_scope(principal)
        rows = [
            {"quarter": "2026-Q1", "revenue": 3_280_000, "contract_count": 17},
            {"quarter": "2026-Q2", "revenue": 4_150_000, "contract_count": 21},
            {"quarter": "2026-Q3", "revenue": 3_760_000, "contract_count": 19},
        ]
        return ToolResult(
            tool_name="sales_metrics",
            route=Route.SQL,
            data=rows[: params.limit],
            row_count=len(rows),
            elapsed_ms=int((perf_counter() - started) * 1000),
            citations=[
                Citation(
                    source_type="sql",
                    source_id=f"sales_metrics:{request_id}",
                    label="CRM 合同聚合视图（Mock）",
                    locator="sales_contract_fact / permitted_sales_scope",
                )
            ],
            simulated=True,
        )

    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult:
        started = perf_counter()
        self._require_scope(principal)
        name = params.entity_name or params.start_entity_id or "华东智造"
        paths = [
            {
                "path": [name, "SIGNED", "HT-2026-018", "CONTAINS", "工业视觉平台"],
                "hops": 2,
                "status": "active",
            }
        ]
        return ToolResult(
            tool_name="graph_relations",
            route=Route.GRAPH,
            data=paths,
            row_count=len(paths),
            confidence=0.6,
            elapsed_ms=int((perf_counter() - started) * 1000),
            simulated=True,
            citations=[
                Citation(
                    source_type="graph",
                    source_id="snapshot:demo-v1:path:001",
                    label="客户—合同—产品关系（模拟演示数据）",
                    locator=f"max_hops={params.max_hops}",
                )
            ],
        )

    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult:
        started = perf_counter()
        self._require_scope(principal)
        chunks = [
            {
                "document_id": "visit-note-2026-0912",
                "chunk_id": "chunk-03",
                "title": "华东智造客户拜访纪要",
                "text": "客户计划第四季度追加产线升级预算，重点关注交付周期和本地化运维。",
                "score": 0.91,
            }
        ]
        return ToolResult(
            tool_name="search_documents",
            route=Route.RAG,
            data=chunks[: params.rerank_top_k],
            row_count=len(chunks),
            confidence=0.91,
            elapsed_ms=int((perf_counter() - started) * 1000),
            simulated=True,
            citations=[
                Citation(
                    source_type="document",
                    source_id="visit-note-2026-0912#chunk-03",
                    label="华东智造客户拜访纪要（模拟演示数据）",
                    locator="第 3 段",
                    excerpt=chunks[0]["text"],
                )
            ],
        )

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        self._require_scope(principal)
        filename = f"sales-analysis-{request_id}.xlsx"
        return ToolResult(
            tool_name="export_report",
            route=Route.EXPORT,
            data={"status": "queued", "filename": filename, "download_url": f"/exports/{filename}"},
            simulated=True,
            citations=[
                Citation(
                    source_type="export",
                    source_id=f"export:{request_id}",
                    label="分析报表导出任务（模拟演示数据）",
                )
            ],
        )

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        del request_id
        self._require_scope(principal)
        started = perf_counter()
        content_hash, chunks = chunk_document(
            principal,
            document,
            chunk_size_chars=1_200,
            overlap_chars=160,
        )
        return DocumentIngestResponse(
            document_id=document.document_id,
            content_hash=content_hash,
            chunk_count=len(chunks),
            row_count=len(chunks),
            status="simulated",
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

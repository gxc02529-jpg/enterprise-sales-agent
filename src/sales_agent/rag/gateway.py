from __future__ import annotations

from time import perf_counter
from typing import Any

from sales_agent.contracts import (
    Citation,
    DocumentDeleteRequest,
    DocumentDeleteResponse,
    DocumentIngestRequest,
    DocumentIngestResponse,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.rag.milvus import MilvusRagService
from sales_agent.tools.gateway import ToolGateway


class MilvusRagGateway(ToolGateway):
    def __init__(self, inner: ToolGateway, rag: MilvusRagService) -> None:
        self.inner = inner
        self.rag = rag

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
        del request_id
        started = perf_counter()
        rows = await self.rag.search(principal, params)
        citations = [
            Citation(
                source_type="document",
                source_id=f"{row['document_id']}#{row['chunk_id']}",
                label=str(row.get("title") or row["document_id"]),
                locator=f"chunk={row.get('chunk_index', 0)};version={row.get('version', '1')}",
                excerpt=str(row.get("text", ""))[:500],
            )
            for row in rows
        ]
        confidence = max((float(row.get("score", 0)) for row in rows), default=0.0)
        return ToolResult(
            tool_name="search_documents",
            route=Route.RAG,
            data=rows,
            citations=citations,
            row_count=len(rows),
            confidence=min(1.0, max(0.0, confidence)),
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        del request_id
        return await self.rag.ingest(principal, document)

    async def delete_document(
        self, principal: Principal, document: DocumentDeleteRequest, request_id: str
    ) -> DocumentDeleteResponse:
        del request_id
        return await self.rag.delete(principal, document)

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        return await self.inner.export_report(principal, payload, request_id)

    async def health(self) -> dict[str, Any]:
        details = await self.inner.health()
        return {**details, "rag_backend": "milvus"}

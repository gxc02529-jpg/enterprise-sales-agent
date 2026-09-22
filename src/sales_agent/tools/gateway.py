from __future__ import annotations

import json
from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any

from sales_agent.contracts import (
    DocumentIngestRequest,
    DocumentIngestResponse,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.observability import MetricsCollector
from sales_agent.resilience import ResiliencePolicy, ResilientExecutor


class ToolGateway(ABC):
    """Only business parameters cross this boundary; raw SQL/nGQL is deliberately absent."""

    @abstractmethod
    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult: ...

    @abstractmethod
    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult: ...

    @abstractmethod
    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult: ...

    @abstractmethod
    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult: ...

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        raise NotImplementedError("document ingestion is not configured")

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": type(self).__name__}


class MCPToolGateway(ToolGateway):
    """FastMCP client adapter used when TOOL_BACKEND=mcp."""

    def __init__(self, url: str, service_token: str) -> None:
        self.url = url
        self.service_token = service_token

    @staticmethod
    def _principal(principal: Principal) -> dict[str, Any]:
        return principal.model_dump()

    @staticmethod
    def _to_payload(result: Any) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if structured:
            return structured
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            return data
        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("MCP tool returned no structured JSON payload")

    async def _call_payload(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from fastmcp import Client

        started = perf_counter()
        async with Client(self.url, auth=self.service_token) as client:
            result = await client.call_tool(name, arguments)
        payload = self._to_payload(result)
        payload["elapsed_ms"] = max(
            int(payload.get("elapsed_ms", 0)), int((perf_counter() - started) * 1000)
        )
        return payload

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        payload = await self._call_payload(name, arguments)
        parsed = ToolResult.model_validate(payload)
        return parsed

    async def health(self) -> dict[str, Any]:
        from fastmcp import Client

        required = {
            "sales_metrics",
            "graph_relations",
            "search_documents",
            "export_report",
            "ingest_document",
        }
        async with Client(self.url, auth=self.service_token) as client:
            tools = await client.list_tools()
        available = {tool.name for tool in tools}
        missing = sorted(required - available)
        return {
            "status": "ok" if not missing else "degraded",
            "backend": "mcp",
            "missing_tools": missing,
        }

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        return await self._call(
            "sales_metrics",
            {
                "principal": self._principal(principal),
                "params": params.model_dump(mode="json"),
                "request_id": request_id,
            },
        )

    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult:
        return await self._call(
            "graph_relations",
            {
                "principal": self._principal(principal),
                "params": params.model_dump(mode="json"),
                "request_id": request_id,
            },
        )

    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult:
        return await self._call(
            "search_documents",
            {
                "principal": self._principal(principal),
                "params": params.model_dump(mode="json"),
                "request_id": request_id,
            },
        )

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        return await self._call(
            "export_report",
            {"principal": self._principal(principal), "payload": payload, "request_id": request_id},
        )

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        payload = await self._call_payload(
            "ingest_document",
            {
                "principal": self._principal(principal),
                "document": document.model_dump(mode="json"),
                "request_id": request_id,
            },
        )
        return DocumentIngestResponse.model_validate(payload)


def route_for_tool(tool_name: str) -> Route:
    return {
        "sales_metrics": Route.SQL,
        "graph_relations": Route.GRAPH,
        "search_documents": Route.RAG,
        "export_report": Route.EXPORT,
    }[tool_name]


class ResilientToolGateway(ToolGateway):
    """Applies isolated policies around each business tool boundary."""

    def __init__(
        self,
        inner: ToolGateway,
        policy: ResiliencePolicy,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.inner = inner
        self.metrics = metrics
        self.executors = {
            name: ResilientExecutor(f"tool:{name}", policy)
            for name in (
                "sales_metrics",
                "graph_relations",
                "search_documents",
                "export_report",
                "ingest_document",
            )
        }

    async def _invoke(self, name: str, fn: Any, *, retryable: bool = True) -> Any:
        started = perf_counter()
        try:
            result = await self.executors[name].run(fn, retryable=retryable)
            if self.metrics is not None:
                self.metrics.record_tool(name, "success", perf_counter() - started)
            return result
        except Exception:
            if self.metrics is not None:
                self.metrics.record_tool(name, "error", perf_counter() - started)
            raise
        finally:
            if self.metrics is not None:
                self._sync_circuit_gauges()

    def _sync_circuit_gauges(self) -> None:
        if self.metrics is None:
            return
        for name, executor in self.executors.items():
            self.metrics.set_circuit(name, str(executor.snapshot()["state"]))

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        return await self._invoke(
            "sales_metrics",
            lambda: self.inner.sales_metrics(principal, params, request_id),
        )

    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult:
        return await self._invoke(
            "graph_relations",
            lambda: self.inner.graph_relations(principal, params, request_id),
        )

    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult:
        return await self._invoke(
            "search_documents",
            lambda: self.inner.search_documents(principal, params, request_id),
        )

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        # Creating an export is not guaranteed to be idempotent. Do not retry it here.
        return await self._invoke(
            "export_report",
            lambda: self.inner.export_report(principal, payload, request_id),
            retryable=False,
        )

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        return await self._invoke(
            "ingest_document",
            lambda: self.inner.ingest_document(principal, document, request_id),
            retryable=False,
        )

    async def health(self) -> dict[str, Any]:
        details = await self.inner.health()
        return {
            **details,
            "resilience": {
                name: executor.snapshot() for name, executor in self.executors.items()
            },
        }

    def resilience_snapshot(self) -> dict[str, dict[str, object]]:
        return {name: executor.snapshot() for name, executor in self.executors.items()}

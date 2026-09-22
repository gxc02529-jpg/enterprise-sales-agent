from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from sales_agent.audit import AuditEvent, build_audit_sink, payload_digest
from sales_agent.config import get_settings
from sales_agent.contracts import (
    DocumentIngestRequest,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    SalesMetricParams,
)
from sales_agent.logging import configure_logging, logger
from sales_agent.tools.factory import build_data_gateway

settings = get_settings()
auth = StaticTokenVerifier(
    tokens={settings.mcp_service_token: {"sub": "sales-agent-api", "client_id": "sales-agent-api"}}
)
mcp = FastMCP("enterprise-sales-tools", auth=auth)
backend = build_data_gateway(settings)
audit_sink = build_audit_sink(settings)


async def _audit_tool(
    tool_name: str,
    principal: Principal,
    request_id: str,
    input_payload: Any,
    output_payload: Any,
    elapsed_ms: int,
    status: str = "completed",
) -> None:
    await audit_sink.emit(
        AuditEvent(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            request_id=request_id,
            event_type="mcp_tool_call",
            tool_name=tool_name,
            input_digest=payload_digest(input_payload),
            output_digest=payload_digest(output_payload),
            status=status,
            elapsed_ms=elapsed_ms,
        )
    )


async def _execute_tool(
    tool_name: str,
    principal: Principal,
    request_id: str,
    input_payload: Any,
    operation: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    started = perf_counter()
    try:
        result = await operation()
    except Exception as exc:
        elapsed_ms = int((perf_counter() - started) * 1000)
        await _audit_tool(
            tool_name,
            principal,
            request_id,
            input_payload,
            {"error_type": type(exc).__name__},
            elapsed_ms,
            status="failed",
        )
        logger.error(
            "mcp_tool_call_failed",
            tool=tool_name,
            request_id=request_id,
            user_id=principal.user_id,
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms,
        )
        raise
    output = result.model_dump(mode="json")
    await _audit_tool(
        tool_name,
        principal,
        request_id,
        input_payload,
        output,
        result.elapsed_ms,
    )
    logger.info(
        "mcp_tool_call",
        tool=tool_name,
        request_id=request_id,
        user_id=principal.user_id,
        row_count=result.row_count,
        elapsed_ms=result.elapsed_ms,
    )
    return output


@mcp.tool(tags={"sales", "read-only"})
async def sales_metrics(
    principal: Principal, params: SalesMetricParams, request_id: str
) -> dict[str, Any]:
    """Aggregate allowed sales facts. Raw SQL is never accepted."""
    return await _execute_tool(
        "sales_metrics",
        principal,
        request_id,
        params.model_dump(mode="json"),
        lambda: backend.sales_metrics(principal, params, request_id),
    )


@mcp.tool(tags={"graph", "read-only"})
async def graph_relations(
    principal: Principal, params: GraphQueryParams, request_id: str
) -> dict[str, Any]:
    """Run an allow-listed, parameterized relation traversal. Raw nGQL is never accepted."""
    return await _execute_tool(
        "graph_relations",
        principal,
        request_id,
        params.model_dump(mode="json"),
        lambda: backend.graph_relations(principal, params, request_id),
    )


@mcp.tool(tags={"rag", "read-only"})
async def search_documents(
    principal: Principal, params: RagQueryParams, request_id: str
) -> dict[str, Any]:
    """Hybrid-search only documents visible to the current identity."""
    return await _execute_tool(
        "search_documents",
        principal,
        request_id,
        params.model_dump(mode="json"),
        lambda: backend.search_documents(principal, params, request_id),
    )


@mcp.tool(tags={"export", "write"})
async def export_report(
    principal: Principal, payload: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """Create an auditable export job from already-authorized analysis results."""
    return await _execute_tool(
        "export_report",
        principal,
        request_id,
        payload,
        lambda: backend.export_report(principal, payload, request_id),
    )


@mcp.tool(tags={"rag", "admin", "write"})
async def ingest_document(
    principal: Principal, document: DocumentIngestRequest, request_id: str
) -> dict[str, Any]:
    """Index one authorized, already-extracted sales document; raw vector operations are hidden."""
    if "admin" not in principal.roles and "knowledge_admin" not in principal.roles:
        raise PermissionError("knowledge_admin or admin role required")
    return await _execute_tool(
        "ingest_document",
        principal,
        request_id,
        document.model_dump(mode="json"),
        lambda: backend.ingest_document(principal, document, request_id),
    )


@mcp.tool(tags={"admin", "observability", "read-only"})
async def resilience_status(principal: Principal) -> dict[str, Any]:
    """Return sanitized tool resilience state for administrators."""
    if "admin" not in principal.roles:
        raise PermissionError("admin role required")
    snapshot = getattr(backend, "resilience_snapshot", lambda: {})()
    return {"tools": snapshot}


def run() -> None:
    configure_logging(settings.log_level)
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port, path="/mcp")


if __name__ == "__main__":
    run()

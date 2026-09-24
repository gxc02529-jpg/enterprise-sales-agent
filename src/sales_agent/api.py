from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from sales_agent.agent.graph import build_sales_graph
from sales_agent.audit import AuditEvent, build_audit_sink
from sales_agent.config import Settings, get_settings
from sales_agent.contracts import (
    AnalysisRequest,
    AnalysisResponse,
    ClarificationResumeRequest,
    DocumentDeleteRequest,
    DocumentDeleteResponse,
    DocumentIngestionJob,
    DocumentIngestionJobPage,
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentJobMetadata,
    DocumentVersionPage,
    LLMConfigView,
    LLMProbeResponse,
    MemoryCandidateRequest,
    MemoryRecord,
    Principal,
)
from sales_agent.intent import build_intent_router
from sales_agent.llm.provider import build_llm_provider, config_view
from sales_agent.logging import configure_logging, logger
from sales_agent.memory_factory import build_memory_service
from sales_agent.observability import MetricsCollector
from sales_agent.prompts import build_prompt_registry
from sales_agent.rag.ingestion import (
    DocumentTooLargeError,
    IngestionQueueFullError,
)
from sales_agent.rag.ingestion_factory import build_ingestion_coordinator
from sales_agent.rag.parsers import DocumentParseError
from sales_agent.rate_limit import (
    AdmissionController,
    ApiConcurrencyExceeded,
    RateLimitExceeded,
    build_admission_controller,
)
from sales_agent.resilience import BulkheadFullError, CircuitOpenError
from sales_agent.security import get_principal, require_admin
from sales_agent.tools.factory import build_resilient_gateway
from sales_agent.tools.gateway import MCPToolGateway, ToolGateway
from sales_agent.tools.mock import MockToolGateway


def _gateway(settings: Settings, metrics: MetricsCollector | None = None) -> ToolGateway:
    if settings.tool_backend == "mcp":
        backend: ToolGateway = MCPToolGateway(
            settings.mcp_server_url, settings.mcp_service_token
        )
    else:
        backend = MockToolGateway(settings.export_dir)
    return build_resilient_gateway(backend, settings, metrics)


def _input(payload: AnalysisRequest, principal: Principal) -> dict[str, Any]:
    return {
        "request_id": payload.request_id,
        "session_id": payload.session_id,
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "roles": principal.roles,
        "scope_tags": principal.scope_tags,
        "query": payload.query,
        "locale": payload.locale,
        "routes": [],
        "routing_confidence": 0.0,
        "routing_backend": "unresolved",
        "resolved_entity_name": "",
        "clarification": None,
        "clarification_answers": {},
        "memory_context": [],
        "tool_results": [],
        "citations": [],
        "answer": "",
        "errors": [],
        "warnings": [],
        "llm_usage": {},
        "attempts": 0,
        "status": "running",
    }


def _response(state: dict[str, Any]) -> AnalysisResponse:
    tool_results = state.get("tool_results", [])
    confidences = [
        float(tr["confidence"])
        for tr in tool_results
        if isinstance(tr, dict) and "confidence" in tr
    ]
    confidence = min(confidences) if confidences else 1.0
    return AnalysisResponse(
        request_id=state["request_id"],
        session_id=state["session_id"],
        answer=state.get("answer", ""),
        routes=state.get("routes", []),
        citations=state.get("citations", []),
        status=state.get("status", "failed"),
        clarification=state.get("clarification"),
        tool_results=tool_results,
        warnings=state.get("warnings", []),
        llm_usage=state.get("llm_usage", {}),
        confidence=confidence,
    )


def _form_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(decoded, list):
        raise ValueError("expected a JSON array or comma-separated string")
    return [str(item) for item in decoded]


class AdmittedPrincipal:
    """FastAPI dependency that holds an admission lease for the whole response."""

    def __init__(self, controller: AdmissionController) -> None:
        self.controller = controller

    async def __call__(
        self,
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AsyncIterator[Principal]:
        key = f"{principal.tenant_id}:{principal.user_id}"
        async with self.controller.admit(key):
            yield principal


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    memory = build_memory_service(settings)
    metrics = MetricsCollector() if settings.metrics_enabled else None
    gateway = _gateway(settings, metrics)
    audit_sink = build_audit_sink(settings)
    llm_provider = build_llm_provider(settings)
    intent_router = build_intent_router(settings, llm_provider)
    prompt_registry = build_prompt_registry(settings)
    admission = build_admission_controller(settings)
    ingestion = build_ingestion_coordinator(gateway, settings)

    admitted_principal = AdmittedPrincipal(admission)
    admitted_dependency = Depends(admitted_principal)

    def compile_graph(checkpointer: Any | None = None) -> Any:
        return build_sales_graph(
            gateway,
            memory,
            memory_token_budget=settings.memory_token_budget,
            max_tool_iterations=settings.max_tool_iterations,
            checkpointer=checkpointer,
            llm=(llm_provider if settings.llm_reasoning_backend == "llm" else None),
            intent_router=intent_router,
            intent_confidence_threshold=settings.intent_confidence_threshold,
            llm_budget_tokens=settings.llm_budget_tokens,
        )

    @asynccontextmanager
    async def lifespan(runtime_app: FastAPI) -> AsyncIterator[None]:
        await ingestion.start()
        try:
            if settings.checkpointer_backend == "postgres":
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                async with AsyncPostgresSaver.from_conn_string(
                    settings.langgraph_database_url
                ) as checkpointer:
                    await checkpointer.setup()
                    runtime_app.state.graph = compile_graph(checkpointer)
                    logger.info("checkpointer_ready", backend="postgres")
                    yield
            else:
                yield
        finally:
            await ingestion.close()
            close_memory = getattr(memory, "close", None)
            if close_memory is not None:
                await close_memory()
            close_audit = getattr(audit_sink, "close", None)
            if close_audit is not None:
                await close_audit()
            if llm_provider is not None:
                await llm_provider.close()
            await admission.close()

    app = FastAPI(
        title="Enterprise Sales Analytics Agent",
        version="0.1.0",
        description="LangGraph orchestration with parameterized FastMCP tools and grounded output.",
        lifespan=lifespan,
    )
    app.state.graph = compile_graph()
    app.state.settings = settings
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(exc.retry_after_seconds)},
            content={"detail": "rate limit exceeded", "code": "RATE_LIMITED"},
        )

    @app.exception_handler(ApiConcurrencyExceeded)
    async def api_saturated(_: Request, __: ApiConcurrencyExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            content={"detail": "request capacity exhausted", "code": "API_SATURATED"},
        )

    @app.exception_handler(CircuitOpenError)
    async def circuit_open(_: Request, exc: CircuitOpenError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": str(max(1, int(exc.retry_after_seconds)))},
            content={"detail": "downstream dependency unavailable", "code": "CIRCUIT_OPEN"},
        )

    @app.exception_handler(BulkheadFullError)
    async def dependency_saturated(_: Request, __: BulkheadFullError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "1"},
            content={"detail": "downstream capacity exhausted", "code": "DEPENDENCY_SATURATED"},
        )

    @app.exception_handler(DocumentTooLargeError)
    async def document_too_large(_: Request, __: DocumentTooLargeError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "document exceeds size limit", "code": "DOCUMENT_TOO_LARGE"},
        )

    @app.exception_handler(IngestionQueueFullError)
    async def ingestion_saturated(_: Request, __: IngestionQueueFullError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": "5"},
            content={"detail": "ingestion queue is full", "code": "INGESTION_SATURATED"},
        )

    @app.exception_handler(DocumentParseError)
    async def invalid_document(_: Request, exc: DocumentParseError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "document cannot be parsed", "code": str(exc)},
        )

    @app.middleware("http")
    async def audit_request(request: Request, call_next: Any) -> Any:
        started = perf_counter()
        response = None
        failure: Exception | None = None
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            failure = exc
            raise
        finally:
            principal = getattr(request.state, "principal", None)
            request_id = getattr(request.state, "request_id", None)
            request_id = request_id or request.headers.get("x-request-id") or str(uuid4())
            status_code = response.status_code if response is not None else 500
            elapsed_ms = int((perf_counter() - started) * 1000)
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                request_id=request_id,
            )
            if metrics is not None:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                metrics.record_http(request.method, route, status_code, elapsed_ms / 1000)
            await audit_sink.emit(
                AuditEvent(
                    tenant_id=principal.tenant_id if principal else "system",
                    user_id=principal.user_id if principal else "anonymous",
                    request_id=request_id,
                    session_id=getattr(request.state, "session_id", None),
                    event_type="http_request",
                    status=(
                        "failed" if failure or status_code >= 400 else "completed"
                    ),
                    elapsed_ms=elapsed_ms,
                    metadata={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": status_code,
                        "error_type": type(failure).__name__ if failure else None,
                    },
                )
            )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        if metrics is None:
            return Response(
                content=b"# metrics disabled\n",
                media_type="text/plain; charset=utf-8",
            )
        return Response(content=metrics.render(), media_type=metrics.content_type)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "tool_backend": settings.tool_backend,
            "checkpointer_backend": settings.checkpointer_backend,
        }

    @app.get("/", response_class=HTMLResponse)
    async def web_ui() -> HTMLResponse:
        html_path = Path(__file__).with_name("webui.html")
        html = html_path.read_text(encoding="utf-8")
        return HTMLResponse(html, media_type="text/html")

    @app.get("/ready")
    async def ready() -> JSONResponse:
        try:
            details = await gateway.health()
            ingestion_details = await ingestion.health()
            details = {**details, "ingestion": ingestion_details}
            ready_status = (
                details.get("status") == "ok"
                and ingestion_details.get("status") == "ok"
            )
        except Exception as exc:
            logger.error("readiness_failed", error_type=type(exc).__name__)
            details = {"status": "unavailable", "error_type": type(exc).__name__}
            ready_status = False
        return JSONResponse(
            status_code=200 if ready_status else 503,
            content=details,
        )

    @app.get("/v1/admin/llm/config", response_model=LLMConfigView)
    async def get_llm_config(
        _: Annotated[Principal, Depends(require_admin)],
    ) -> LLMConfigView:
        return config_view(settings)

    @app.get("/v1/admin/prompts")
    async def list_prompts(
        _: Annotated[Principal, Depends(require_admin)],
    ) -> dict[str, Any]:
        templates = prompt_registry.list_templates()
        return {
            "catalog": str(settings.prompt_catalog_path),
            "default_locale": settings.prompt_default_locale,
            "items": [template.model_dump(mode="json") for template in templates],
        }

    @app.post("/v1/admin/llm/probe", response_model=LLMProbeResponse)
    async def probe_llm(
        _: Annotated[Principal, Depends(require_admin)],
    ) -> LLMProbeResponse:
        if llm_provider is None:
            return LLMProbeResponse(
                status="not_configured",
                provider=settings.llm_provider,
                model=settings.llm_model,
                detail="LLM_API_KEY is not configured",
            )
        return await llm_provider.probe()

    @app.get("/v1/admin/resilience")
    async def resilience_status(
        _: Annotated[Principal, Depends(require_admin)],
    ) -> dict[str, Any]:
        gateway_snapshot = getattr(gateway, "resilience_snapshot", lambda: {})()
        llm_snapshot = (
            getattr(llm_provider, "resilience_snapshot", lambda: None)()
            if llm_provider is not None
            else None
        )
        return {
            "api_admission": admission.snapshot(),
            "tools": gateway_snapshot,
            "llm": llm_snapshot,
            "ingestion": ingestion.snapshot(),
        }

    @app.post("/v1/admin/documents/ingest", response_model=DocumentIngestResponse)
    async def ingest_document(
        payload: DocumentIngestRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_admin)],
    ) -> DocumentIngestResponse:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id
        return await gateway.ingest_document(principal, payload, request_id)

    @app.delete(
        "/v1/admin/documents/{document_id}", response_model=DocumentDeleteResponse
    )
    async def delete_document(
        document_id: str,
        payload: DocumentDeleteRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_admin)],
    ) -> DocumentDeleteResponse:
        if payload.document_id != document_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "DOCUMENT_ID_MISMATCH"},
            )
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id
        return await gateway.delete_document(principal, payload, request_id)

    @app.get(
        "/v1/admin/documents/{document_id}/versions",
        response_model=DocumentVersionPage,
    )
    async def list_document_versions(
        document_id: str,
        principal: Annotated[Principal, Depends(require_admin)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> DocumentVersionPage:
        return await ingestion.list_versions(principal, document_id, limit=limit)

    @app.post(
        "/v1/admin/documents/jobs",
        response_model=DocumentIngestionJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_document_job(
        request: Request,
        principal: Annotated[Principal, Depends(require_admin)],
        file: Annotated[UploadFile, File()],
        document_id: Annotated[str, Form()],
        title: Annotated[str, Form()],
        document_type: Annotated[str, Form()],
        customer_ids: Annotated[str, Form()] = "[]",
        permission_tags: Annotated[str, Form()] = "[]",
        owner_user_id: Annotated[str | None, Form()] = None,
        source_uri: Annotated[str | None, Form()] = None,
        version: Annotated[str, Form()] = "1",
        source_updated_at: Annotated[datetime | None, Form()] = None,
    ) -> DocumentIngestionJob:
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id
        try:
            metadata = DocumentJobMetadata(
                document_id=document_id,
                title=title,
                document_type=document_type,
                customer_ids=_form_list(customer_ids),
                permission_tags=_form_list(permission_tags),
                owner_user_id=owner_user_id,
                source_uri=source_uri,
                version=version,
                source_updated_at=source_updated_at,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "INVALID_DOCUMENT_METADATA"},
            ) from exc
        content = await file.read(settings.ingestion_max_file_bytes + 1)
        return await ingestion.submit(
            principal,
            metadata,
            filename=file.filename or document_id,
            media_type=file.content_type or "application/octet-stream",
            content=content,
        )

    @app.get("/v1/admin/documents/jobs", response_model=DocumentIngestionJobPage)
    async def list_document_jobs(
        principal: Annotated[Principal, Depends(require_admin)],
        job_status: Annotated[
            Literal[
                "queued",
                "parsing",
                "indexing",
                "completed",
                "failed",
                "dead_letter",
            ]
            | None,
            Query(alias="status"),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> DocumentIngestionJobPage:
        jobs = await ingestion.list_jobs(principal, status=job_status, limit=limit)
        return DocumentIngestionJobPage(items=jobs, count=len(jobs))

    @app.get("/v1/admin/documents/jobs/{job_id}", response_model=DocumentIngestionJob)
    async def get_document_job(
        job_id: UUID,
        principal: Annotated[Principal, Depends(require_admin)],
    ) -> DocumentIngestionJob:
        try:
            return await ingestion.get(principal, str(job_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="document ingestion job not found",
            ) from exc

    @app.post(
        "/v1/admin/documents/jobs/{job_id}/retry",
        response_model=DocumentIngestionJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_document_job(
        job_id: UUID,
        principal: Annotated[Principal, Depends(require_admin)],
    ) -> DocumentIngestionJob:
        try:
            return await ingestion.retry(principal, str(job_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "DOCUMENT_JOB_NOT_RETRYABLE"},
            ) from exc

    @app.post("/v1/analyze", response_model=AnalysisResponse)
    async def analyze(
        payload: AnalysisRequest,
        request: Request,
        principal: Principal = admitted_dependency,
    ) -> AnalysisResponse:
        request.state.request_id = payload.request_id
        request.state.session_id = payload.session_id
        thread_id = f"{principal.tenant_id}:{principal.user_id}:{payload.session_id}"
        config = {"configurable": {"thread_id": thread_id}}
        try:
            async with asyncio.timeout(settings.api_request_timeout_seconds):
                state = await request.app.state.graph.ainvoke(
                    _input(payload, principal), config=config
                )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail={"code": "REQUEST_TIMEOUT", "message": "analysis deadline exceeded"},
            ) from exc
        return _response(state)

    @app.post("/v1/analyze/clarify", response_model=AnalysisResponse)
    async def resume_analysis(
        payload: ClarificationResumeRequest,
        request: Request,
        principal: Principal = admitted_dependency,
    ) -> AnalysisResponse:
        request.state.session_id = payload.session_id
        thread_id = f"{principal.tenant_id}:{principal.user_id}:{payload.session_id}"
        config = {"configurable": {"thread_id": thread_id}}
        try:
            async with asyncio.timeout(settings.api_request_timeout_seconds):
                state = await request.app.state.graph.ainvoke(
                    Command(resume=payload.answers), config=config
                )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail={"code": "REQUEST_TIMEOUT", "message": "analysis deadline exceeded"},
            ) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "NO_PENDING_CLARIFICATION"},
            ) from exc
        request.state.request_id = state.get("request_id")
        return _response(state)

    @app.post("/v1/analyze/stream")
    async def analyze_stream(
        payload: AnalysisRequest,
        request: Request,
        principal: Principal = admitted_dependency,
    ) -> StreamingResponse:
        request.state.request_id = payload.request_id
        request.state.session_id = payload.session_id

        async def events() -> AsyncIterator[str]:
            thread_id = f"{principal.tenant_id}:{principal.user_id}:{payload.session_id}"
            config = {"configurable": {"thread_id": thread_id}}
            last: dict[str, Any] = {}
            yield (
                "event: accepted\ndata: "
                + json.dumps({"request_id": payload.request_id}, ensure_ascii=False)
                + "\n\n"
            )
            try:
                async with asyncio.timeout(settings.api_request_timeout_seconds):
                    async for state in request.app.state.graph.astream(
                        _input(payload, principal), config=config, stream_mode="values"
                    ):
                        last = state
                        progress = {
                            "status": state.get("status", "running"),
                            "routes": [str(route) for route in state.get("routes", [])],
                            "tool_count": len(state.get("tool_results", [])),
                            "attempts": state.get("attempts", 0),
                        }
                        yield (
                            "event: progress\ndata: "
                            + json.dumps(progress, ensure_ascii=False)
                            + "\n\n"
                        )
            except TimeoutError:
                failure = {
                    "request_id": payload.request_id,
                    "code": "REQUEST_TIMEOUT",
                    "message": "analysis deadline exceeded",
                }
                yield "event: failed\ndata: " + json.dumps(failure, ensure_ascii=False) + "\n\n"
                return
            final = _response(last).model_dump(mode="json")
            event_name = (
                "clarification_required"
                if final["status"] == "needs_clarification"
                else "completed"
            )
            yield (
                f"event: {event_name}\ndata: "
                + json.dumps(final, ensure_ascii=False)
                + "\n\n"
            )

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/memories/candidates", response_model=MemoryRecord)
    async def propose_memory(
        payload: MemoryCandidateRequest,
        principal: Principal = admitted_dependency,
    ) -> MemoryRecord:
        return await memory.propose_user_memory(principal, payload.content)

    @app.post("/v1/memories/candidates/{memory_id}/confirm", response_model=MemoryRecord)
    async def confirm_memory(
        memory_id: str,
        principal: Principal = admitted_dependency,
    ) -> MemoryRecord:
        try:
            return await memory.confirm_user_memory(principal, memory_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory candidate not found",
            ) from exc

    export_dir = settings.export_dir.resolve()
    export_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/exports", StaticFiles(directory=str(export_dir)), name="exports")

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("sales_agent.api:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    run()

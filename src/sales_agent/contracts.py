from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Route(StrEnum):
    SQL = "sql"
    GRAPH = "graph"
    RAG = "rag"
    EXPORT = "export"


class Principal(BaseModel):
    user_id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    scope_tags: list[str] = Field(default_factory=list)


class AnalysisRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8_000)
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    locale: str = "zh-CN"


class Citation(BaseModel):
    source_type: Literal["sql", "graph", "document", "memory", "export"]
    source_id: str
    label: str
    locator: str | None = None
    excerpt: str | None = None


class ToolResult(BaseModel):
    tool_name: str
    route: Route
    data: dict[str, Any] | list[dict[str, Any]]
    citations: list[Citation] = Field(default_factory=list)
    row_count: int | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    elapsed_ms: int = 0
    cache_hit: bool = False
    simulated: bool = False


class AnalysisResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    routes: list[Route]
    citations: list[Citation]
    status: Literal["completed", "needs_human_review", "failed"]
    tool_results: list[ToolResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm_usage: dict[str, int] = Field(default_factory=dict)


class MemoryRecord(BaseModel):
    memory_id: str
    layer: Literal["session", "user", "business"]
    content: str
    owner_user_id: str | None = None
    tenant_id: str
    permission_tags: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    status: Literal["candidate", "active", "rejected"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None


class MemoryCandidateRequest(BaseModel):
    content: str = Field(min_length=2, max_length=2_000)


class LLMConfigView(BaseModel):
    provider: str
    api_style: str
    reasoning_backend: Literal["rules", "llm"]
    base_url: str
    model: str
    router_model: str
    synthesis_model: str
    api_key_configured: bool
    timeout_seconds: int
    connect_timeout_seconds: int
    max_output_tokens: int
    temperature: float
    max_retries: int
    circuit_failure_threshold: int
    circuit_recovery_seconds: int
    max_concurrency: int


class LLMProbeResponse(BaseModel):
    status: Literal["ok", "unavailable", "not_configured"]
    provider: str
    model: str
    elapsed_ms: int = 0
    detail: str | None = None


class SalesMetricParams(BaseModel):
    metrics: list[Literal["revenue", "contract_count", "customer_count", "avg_deal_size"]]
    dimensions: list[Literal["month", "quarter", "salesperson", "customer", "product", "industry"]]
    start_date: date | None = None
    end_date: date | None = None
    customer_ids: list[str] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1_000)


class GraphQueryParams(BaseModel):
    start_entity_id: str | None = None
    entity_name: str | None = None
    relation_types: list[
        Literal["RESPONSIBLE_FOR", "SIGNED", "CONTAINS", "BELONGS_TO", "BENCHMARKS"]
    ] = Field(default_factory=list)
    max_hops: int = Field(default=2, ge=1, le=4)
    limit: int = Field(default=50, ge=1, le=200)


class RagQueryParams(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    customer_ids: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    top_k: int = Field(default=8, ge=1, le=50)
    rerank_top_k: int = Field(default=5, ge=1, le=20)


class DocumentIngestRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    document_type: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2_000_000)
    customer_ids: list[str] = Field(default_factory=list, max_length=100)
    permission_tags: list[str] = Field(default_factory=list, max_length=100)
    owner_user_id: str | None = Field(default=None, max_length=128)
    source_uri: str | None = Field(default=None, max_length=2_048)
    version: str = Field(default="1", max_length=64)


class DocumentIngestResponse(BaseModel):
    document_id: str
    content_hash: str
    chunk_count: int = Field(ge=0)
    status: Literal["indexed", "simulated"]
    elapsed_ms: int = 0
    row_count: int | None = None


class DocumentJobMetadata(BaseModel):
    document_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=512)
    document_type: str = Field(min_length=1, max_length=64)
    customer_ids: list[str] = Field(default_factory=list, max_length=100)
    permission_tags: list[str] = Field(default_factory=list, max_length=100)
    owner_user_id: str | None = Field(default=None, max_length=128)
    source_uri: str | None = Field(default=None, max_length=2_048)
    version: str = Field(default="1", max_length=64)


class DocumentIngestionJob(BaseModel):
    job_id: str
    tenant_id: str
    document_id: str
    filename: str
    media_type: str
    status: Literal[
        "queued", "parsing", "indexing", "completed", "failed", "dead_letter"
    ]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attempt_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    result: DocumentIngestResponse | None = None


class DocumentIngestionJobPage(BaseModel):
    items: list[DocumentIngestionJob]
    count: int = Field(ge=0)

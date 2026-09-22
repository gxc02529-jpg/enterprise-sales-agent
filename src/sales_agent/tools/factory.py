from __future__ import annotations

from sales_agent.config import Settings
from sales_agent.observability import MetricsCollector
from sales_agent.rag.embedding import BGEM3DenseEmbedder
from sales_agent.rag.gateway import MilvusRagGateway
from sales_agent.rag.milvus import MilvusRagService
from sales_agent.rag.reranker import BGEReranker
from sales_agent.resilience import ResiliencePolicy
from sales_agent.tools.export import ExcelExportGateway
from sales_agent.tools.gateway import ResilientToolGateway, ToolGateway
from sales_agent.tools.mock import MockToolGateway
from sales_agent.tools.nebula import NebulaGraphGateway
from sales_agent.tools.postgres import PostgresSalesGateway


def build_data_gateway(settings: Settings, metrics: MetricsCollector | None = None) -> ToolGateway:
    if settings.data_backend == "postgres":
        backend: ToolGateway = PostgresSalesGateway(
            settings.database_url,
            settings.export_dir,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
            command_timeout_seconds=settings.postgres_command_timeout_seconds,
        )
    else:
        backend = MockToolGateway(settings.export_dir)
    if settings.rag_backend == "milvus":
        embedder = BGEM3DenseEmbedder(
            settings.embedding_model,
            device=settings.rag_embedding_device,
            batch_size=settings.rag_embedding_batch_size,
            dimension=settings.rag_embedding_dim,
        )
        reranker = (
            BGEReranker(settings.reranker_model, device=settings.rag_embedding_device)
            if settings.rag_enable_reranker
            else None
        )
        backend = MilvusRagGateway(
            backend,
            MilvusRagService(settings, embedder, reranker),
        )
    if settings.graph_backend == "nebula":
        backend = NebulaGraphGateway(backend, settings)
    if settings.export_backend == "xlsx":
        backend = ExcelExportGateway(backend, settings)
    return build_resilient_gateway(backend, settings, metrics)


def build_resilient_gateway(
    backend: ToolGateway, settings: Settings, metrics: MetricsCollector | None = None
) -> ToolGateway:
    return ResilientToolGateway(
        backend,
        ResiliencePolicy(
            timeout_seconds=settings.tool_timeout_seconds,
            max_retries=settings.tool_max_retries,
            retry_base_delay_seconds=settings.tool_retry_base_delay_seconds,
            failure_threshold=settings.tool_circuit_failure_threshold,
            recovery_timeout_seconds=settings.tool_circuit_recovery_seconds,
            max_concurrency=settings.tool_max_concurrency,
            bulkhead_wait_seconds=settings.tool_bulkhead_wait_seconds,
        ),
        metrics=metrics,
    )

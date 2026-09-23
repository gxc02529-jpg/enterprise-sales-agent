from __future__ import annotations

from typing import Any

from sales_agent.config import Settings
from sales_agent.rag.ingestion import (
    IngestionCoordinator,
    InMemoryIngestionCoordinator,
)
from sales_agent.rag.ingestion_durable import (
    PostgresIngestionJobStore,
    RedisStreamIngestionCoordinator,
)
from sales_agent.tools.gateway import ToolGateway


def build_ingestion_coordinator(
    gateway: ToolGateway,
    settings: Settings,
    *,
    store: Any | None = None,
    redis_client: Any | None = None,
) -> IngestionCoordinator:
    if settings.ingestion_backend == "redis_stream":
        durable_store = store or PostgresIngestionJobStore(
            settings.database_url,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
            command_timeout_seconds=settings.postgres_command_timeout_seconds,
        )
        return RedisStreamIngestionCoordinator(
            gateway,
            redis_url=settings.redis_url,
            stream_name=settings.ingestion_stream_name,
            consumer_group=settings.ingestion_consumer_group,
            max_file_bytes=settings.ingestion_max_file_bytes,
            queue_capacity=settings.ingestion_queue_capacity,
            max_attempts=settings.ingestion_max_attempts,
            claim_idle_seconds=settings.ingestion_claim_idle_seconds,
            poll_block_ms=settings.ingestion_poll_block_ms,
            cleanup_interval_seconds=settings.ingestion_cleanup_interval_seconds,
            failed_payload_retention_days=(
                settings.ingestion_failed_payload_retention_days
            ),
            job_retention_days=settings.ingestion_job_retention_days,
            store=durable_store,
            client=redis_client,
        )
    return InMemoryIngestionCoordinator(
        gateway,
        max_file_bytes=settings.ingestion_max_file_bytes,
        queue_capacity=settings.ingestion_queue_capacity,
    )

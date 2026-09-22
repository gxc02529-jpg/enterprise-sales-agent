from __future__ import annotations

from sales_agent.config import Settings
from sales_agent.memory import InMemoryMemoryService, MemoryService
from sales_agent.memory_postgres import PostgresMemoryService


def build_memory_service(settings: Settings) -> MemoryService:
    if settings.memory_backend == "postgres":
        return PostgresMemoryService(
            settings.database_url,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
            command_timeout_seconds=settings.postgres_command_timeout_seconds,
        )
    return InMemoryMemoryService()

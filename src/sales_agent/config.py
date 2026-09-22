from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings; secrets are supplied only through environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    metrics_enabled: bool = True
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8001

    tool_backend: Literal["mock", "mcp"] = "mock"
    data_backend: Literal["mock", "postgres"] = "mock"
    mcp_server_url: str = "http://localhost:8001/mcp"
    mcp_service_token: str = "change-me"

    database_url: str = "postgresql+asyncpg://sales_agent:sales_agent@localhost:5432/sales_agent"
    langgraph_database_url: str = "postgresql://sales_agent:sales_agent@localhost:5432/sales_agent"
    checkpointer_backend: Literal["memory", "postgres"] = "memory"
    memory_backend: Literal["memory", "postgres"] = "memory"
    audit_backend: Literal["log", "postgres"] = "log"
    redis_url: str = "redis://localhost:6379/0"
    milvus_uri: str = "http://localhost:19530"
    milvus_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    rag_backend: Literal["mock", "milvus"] = "mock"
    rag_collection_name: str = "sales_document_chunks"
    rag_embedding_dim: int = Field(default=1024, ge=8, le=65_536)
    rag_chunk_size_chars: int = Field(default=1_200, ge=200, le=20_000)
    rag_chunk_overlap_chars: int = Field(default=160, ge=0, le=5_000)
    rag_retrieval_top_k: int = Field(default=20, ge=1, le=200)
    rag_rerank_top_k: int = Field(default=5, ge=1, le=50)
    rag_min_score: float = Field(default=0.0, ge=0, le=1)
    rag_embedding_device: str = "cpu"
    rag_embedding_batch_size: int = Field(default=16, ge=1, le=512)
    rag_enable_reranker: bool = True
    nebula_host: str = "localhost"
    nebula_port: int = 9669
    nebula_user: str = "root"
    nebula_password: str = "nebula"
    nebula_space: str = "sales_graph"
    graph_backend: Literal["mock", "nebula"] = "mock"

    jwt_secret: str = "change-this-in-production"
    jwt_algorithm: str = "HS256"
    jwt_audience: str = "sales-agent"
    allow_dev_token: bool = True

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    llm_model: str = "gpt-4.1-mini"
    llm_provider: Literal["openai_compatible"] = "openai_compatible"
    llm_api_style: Literal["chat_completions"] = "chat_completions"
    llm_reasoning_backend: Literal["rules", "llm"] = "rules"
    llm_router_model: str = ""
    llm_synthesis_model: str = ""
    llm_timeout_seconds: int = Field(default=60, ge=1, le=600)
    llm_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    llm_max_output_tokens: int = Field(default=1_200, ge=32, le=32_768)
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    llm_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    llm_circuit_recovery_seconds: int = Field(default=30, ge=1, le=3_600)
    llm_max_concurrency: int = Field(default=8, ge=1, le=1_000)
    llm_bulkhead_wait_seconds: float = Field(default=0.1, ge=0.01, le=30)
    llm_retry_base_delay_seconds: float = Field(default=0.25, ge=0, le=30)
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    session_memory_ttl_seconds: int = 86_400
    memory_token_budget: int = 1_800
    max_tool_iterations: int = 3
    llm_budget_tokens: int = 12_000
    export_dir: Path = Field(default=Path("./exports"))
    export_backend: Literal["mock", "xlsx"] = "mock"
    postgres_pool_min_size: int = Field(default=1, ge=1, le=20)
    postgres_pool_max_size: int = Field(default=10, ge=1, le=100)
    postgres_command_timeout_seconds: int = Field(default=15, ge=1, le=120)
    tool_timeout_seconds: int = Field(default=20, ge=1, le=600)
    tool_max_retries: int = Field(default=1, ge=0, le=10)
    tool_retry_base_delay_seconds: float = Field(default=0.2, ge=0, le=30)
    tool_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    tool_circuit_recovery_seconds: int = Field(default=30, ge=1, le=3_600)
    tool_max_concurrency: int = Field(default=20, ge=1, le=1_000)
    tool_bulkhead_wait_seconds: float = Field(default=0.1, ge=0.01, le=30)
    api_request_timeout_seconds: int = Field(default=90, ge=1, le=3_600)
    api_rate_limit_backend: Literal["memory", "redis"] = "memory"
    api_rate_limit_redis_prefix: str = "sales-agent:admission"
    api_rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    api_max_concurrent_per_user: int = Field(default=4, ge=1, le=1_000)
    api_concurrency_wait_seconds: float = Field(default=0.1, ge=0.01, le=30)
    ingestion_max_file_bytes: int = Field(default=20_000_000, ge=1_024, le=500_000_000)
    ingestion_queue_capacity: int = Field(default=100, ge=1, le=100_000)

    @model_validator(mode="after")
    def reject_unsafe_production_defaults(self) -> Settings:
        if self.app_env != "production":
            return self
        unsafe = {
            "JWT_SECRET": self.jwt_secret == "change-this-in-production",
            "MCP_SERVICE_TOKEN": self.mcp_service_token == "change-me",
            "ALLOW_DEV_TOKEN": self.allow_dev_token,
        }
        invalid = [name for name, is_unsafe in unsafe.items() if is_unsafe]
        if invalid:
            raise ValueError(f"unsafe production settings: {', '.join(invalid)}")
        if self.tool_backend != "mcp":
            raise ValueError("production requires TOOL_BACKEND=mcp")
        if self.data_backend == "mock":
            raise ValueError("production cannot use DATA_BACKEND=mock")
        if self.checkpointer_backend != "postgres":
            raise ValueError("production requires CHECKPOINTER_BACKEND=postgres")
        if self.memory_backend != "postgres":
            raise ValueError("production requires MEMORY_BACKEND=postgres")
        if self.audit_backend != "postgres":
            raise ValueError("production requires AUDIT_BACKEND=postgres")
        if self.rag_backend != "milvus":
            raise ValueError("production requires RAG_BACKEND=milvus")
        if self.api_rate_limit_backend != "redis":
            raise ValueError("production requires API_RATE_LIMIT_BACKEND=redis")
        if self.llm_reasoning_backend == "llm" and not self.llm_api_key.get_secret_value():
            raise ValueError("LLM_API_KEY is required when LLM_REASONING_BACKEND=llm")
        return self

    @model_validator(mode="after")
    def validate_pool_sizes(self) -> Settings:
        if self.postgres_pool_min_size > self.postgres_pool_max_size:
            raise ValueError("POSTGRES_POOL_MIN_SIZE cannot exceed POSTGRES_POOL_MAX_SIZE")
        if self.rag_chunk_overlap_chars >= self.rag_chunk_size_chars:
            raise ValueError("RAG_CHUNK_OVERLAP_CHARS must be smaller than RAG_CHUNK_SIZE_CHARS")
        if self.rag_rerank_top_k > self.rag_retrieval_top_k:
            raise ValueError("RAG_RERANK_TOP_K cannot exceed RAG_RETRIEVAL_TOP_K")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

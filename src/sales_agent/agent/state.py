from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    request_id: str
    session_id: str
    user_id: str
    tenant_id: str
    roles: list[str]
    scope_tags: list[str]
    query: str
    locale: str
    routes: list[str]
    memory_context: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    answer: str
    errors: list[str]
    warnings: list[str]
    llm_usage: dict[str, int]
    attempts: int
    status: Literal["running", "completed", "needs_human_review", "failed"]

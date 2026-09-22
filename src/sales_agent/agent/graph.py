from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from sales_agent.agent.router import route_query
from sales_agent.agent.state import AgentState
from sales_agent.contracts import (
    Citation,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.llm.provider import LLMProvider
from sales_agent.memory import MemoryService
from sales_agent.tools.gateway import ToolGateway


def _principal(state: AgentState) -> Principal:
    return Principal(
        user_id=state["user_id"],
        tenant_id=state["tenant_id"],
        roles=state.get("roles", []),
        scope_tags=state.get("scope_tags", []),
    )


def _entity_name(query: str) -> str | None:
    quoted = re.search(r"[“\"']([^”\"']{2,40})[”\"']", query)
    if quoted:
        return quoted.group(1)
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24}(?:公司|集团|客户))", query)
    return match.group(1) if match else None


def build_expert_subgraph(route: Route, gateway: ToolGateway) -> Any:
    async def call_tool(state: AgentState) -> dict[str, Any]:
        principal = _principal(state)
        query = state["query"]
        request_id = state["request_id"]
        if route == Route.SQL:
            result = await gateway.sales_metrics(
                principal,
                SalesMetricParams(
                    metrics=["revenue", "contract_count"],
                    dimensions=["quarter"],
                ),
                request_id,
            )
        elif route == Route.GRAPH:
            result = await gateway.graph_relations(
                principal,
                GraphQueryParams(entity_name=_entity_name(query), max_hops=2),
                request_id,
            )
        elif route == Route.RAG:
            result = await gateway.search_documents(
                principal,
                RagQueryParams(query=query),
                request_id,
            )
        else:
            prior_results = list(state.get("tool_results", []))
            result = await gateway.export_report(
                principal,
                {"query": query, "prior_results": prior_results},
                request_id,
            )
        return {"tool_results": [result.model_dump(mode="json")]}

    builder = StateGraph(AgentState)
    builder.add_node("call_parameterized_tool", call_tool)
    builder.add_edge(START, "call_parameterized_tool")
    builder.add_edge("call_parameterized_tool", END)
    return builder.compile()


def build_validator_subgraph() -> Any:
    async def validate(state: AgentState) -> dict[str, Any]:
        results = [ToolResult.model_validate(item) for item in state.get("tool_results", [])]
        citations: list[Citation] = []
        warnings = list(state.get("warnings", []))
        seen: set[tuple[str, str]] = set()
        for result in results:
            if result.simulated:
                warnings.append(f"{result.tool_name} 返回的是模拟演示数据（未连接真实后端）")
            if result.confidence < 0.55:
                warnings.append(f"{result.tool_name} 结果置信度较低，建议人工复核")
            for citation in result.citations:
                key = (citation.source_type, citation.source_id)
                if key not in seen:
                    citations.append(citation)
                    seen.add(key)
        if state.get("errors"):
            status = "failed" if not results else "needs_human_review"
        elif results and not citations:
            warnings.append("结果缺少可验证来源")
            status = "needs_human_review"
        else:
            status = "completed"
        return {
            "citations": [citation.model_dump(mode="json") for citation in citations],
            "warnings": warnings,
            "status": status,
        }

    builder = StateGraph(AgentState)
    builder.add_node("enforce_grounding", validate)
    builder.add_edge(START, "enforce_grounding")
    builder.add_edge("enforce_grounding", END)
    return builder.compile()


def build_sales_graph(
    gateway: ToolGateway,
    memory: MemoryService,
    *,
    memory_token_budget: int = 1_800,
    max_tool_iterations: int = 3,
    checkpointer: Any | None = None,
    llm: LLMProvider | None = None,
    llm_budget_tokens: int = 12_000,
) -> Any:
    experts = {route: build_expert_subgraph(route, gateway) for route in Route}
    validator = build_validator_subgraph()

    async def recall_memory(state: AgentState) -> dict[str, Any]:
        records = await memory.recall(
            _principal(state), state["session_id"], state["query"], memory_token_budget
        )
        return {"memory_context": [r.model_dump(mode="json") for r in records]}

    async def route_intent(state: AgentState) -> dict[str, Any]:
        warnings = list(state.get("warnings", []))
        usage = dict(state.get("llm_usage", {}))
        if llm is not None:
            try:
                routes, route_usage = await llm.route(state["query"])
                for key, value in route_usage.items():
                    usage[key] = usage.get(key, 0) + value
                return {
                    "routes": [route.value for route in routes],
                    "status": "running",
                    "warnings": warnings,
                    "llm_usage": usage,
                }
            except Exception:
                warnings.append("LLM 路由不可用，已降级为规则路由")
        return {
            "routes": [route.value for route in route_query(state["query"])],
            "status": "running",
            "warnings": warnings,
            "llm_usage": usage,
        }

    async def dispatch(state: AgentState) -> dict[str, Any]:
        routes = [Route(route) for route in state.get("routes", [])]
        non_export = [route for route in routes if route != Route.EXPORT]
        export_requested = Route.EXPORT in routes
        errors: list[str] = []

        async def invoke(route: Route) -> ToolResult | None:
            try:
                output = await experts[route].ainvoke({**state, "tool_results": []})
                results = output.get("tool_results", [])
                return ToolResult.model_validate(results[-1]) if results else None
            except Exception as exc:  # graph state carries sanitized failure details
                errors.append(f"{route.value}: {type(exc).__name__}: {exc}")
                return None

        first_wave = await asyncio.gather(*(invoke(route) for route in non_export))
        results = [result for result in first_wave if result is not None]
        if export_requested and not errors:
            try:
                export_state = {
                    **state,
                    "tool_results": [result.model_dump(mode="json") for result in results],
                }
                output = await experts[Route.EXPORT].ainvoke(export_state)
                export_results = output.get("tool_results", [])
                if export_results:
                    results.append(ToolResult.model_validate(export_results[-1]))
            except Exception as exc:
                errors.append(f"export: {type(exc).__name__}: {exc}")
        return {
            "tool_results": [result.model_dump(mode="json") for result in results],
            "errors": errors,
            "attempts": state.get("attempts", 0) + 1,
        }

    def retry_or_continue(state: AgentState) -> str:
        if state.get("errors") and state.get("attempts", 0) < max_tool_iterations:
            return "retry"
        return "continue"

    async def synthesize(state: AgentState) -> dict[str, Any]:
        if not state.get("tool_results"):
            errors = "; ".join(state.get("errors", [])) or "未获得工具结果"
            return {"answer": f"本次分析未能完成：{errors}"}
        sections: list[str] = []
        results = [ToolResult.model_validate(item) for item in state["tool_results"]]
        for result in results:
            if result.route == Route.SQL:
                sections.append(f"销售指标：{json.dumps(result.data, ensure_ascii=False)}")
            elif result.route == Route.GRAPH:
                sections.append(f"关系路径：{json.dumps(result.data, ensure_ascii=False)}")
            elif result.route == Route.RAG:
                sections.append(f"文档证据：{json.dumps(result.data, ensure_ascii=False)}")
            else:
                sections.append(f"报表导出：{json.dumps(result.data, ensure_ascii=False)}")
        memory_note = ""
        if state.get("memory_context"):
            memory_note = f"\n已参考 {len(state['memory_context'])} 条有权限的历史记忆。"
        fallback_answer = "\n".join(sections) + memory_note
        if llm is None:
            return {"answer": fallback_answer}
        current_usage = state.get("llm_usage", {}).get("total_tokens", 0)
        if current_usage >= llm_budget_tokens:
            warnings = list(state.get("warnings", []))
            warnings.append("LLM token 预算已耗尽，已返回确定性工具结果")
            return {"answer": fallback_answer, "warnings": warnings}
        try:
            generation = await llm.synthesize(
                state["query"],
                state["tool_results"],
                state.get("memory_context", []),
            )
            usage = dict(state.get("llm_usage", {}))
            for key, value in generation.usage.items():
                usage[key] = usage.get(key, 0) + value
            return {"answer": generation.text, "llm_usage": usage}
        except Exception:
            warnings = list(state.get("warnings", []))
            warnings.append("LLM 结果融合不可用，已返回确定性工具结果")
            return {"answer": fallback_answer, "warnings": warnings}

    async def validate_output(state: AgentState) -> dict[str, Any]:
        validated = await validator.ainvoke(state)
        return {
            "citations": validated.get("citations", []),
            "warnings": validated.get("warnings", []),
            "status": validated.get("status", "needs_human_review"),
        }

    builder = StateGraph(AgentState)
    builder.add_node("memory_pre_recall", recall_memory)
    builder.add_node("intent_router", route_intent)
    builder.add_node("expert_dispatch", dispatch)
    builder.add_node("result_synthesis", synthesize)
    builder.add_node("output_validator", validate_output)
    builder.add_edge(START, "memory_pre_recall")
    builder.add_edge("memory_pre_recall", "intent_router")
    builder.add_edge("intent_router", "expert_dispatch")
    builder.add_conditional_edges(
        "expert_dispatch",
        retry_or_continue,
        {"retry": "expert_dispatch", "continue": "result_synthesis"},
    )
    builder.add_edge("result_synthesis", "output_validator")
    builder.add_edge("output_validator", END)
    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver()
    )

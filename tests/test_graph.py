import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from sales_agent.agent.graph import build_sales_graph
from sales_agent.contracts import RagQueryParams, Route, ToolResult
from sales_agent.llm.provider import LLMGeneration
from sales_agent.memory import InMemoryMemoryService
from sales_agent.tools.mock import MockToolGateway


@pytest.mark.asyncio
async def test_graph_fuses_tools_and_citations() -> None:
    graph = build_sales_graph(MockToolGateway(), InMemoryMemoryService())
    initial = {
        "request_id": "req-1",
        "session_id": "session-1",
        "user_id": "sales-1",
        "tenant_id": "tenant-1",
        "roles": ["sales"],
        "scope_tags": ["sales:sales-1"],
        "query": "统计销售额，查看客户关系以及拜访纪要",
        "locale": "zh-CN",
        "routes": [],
        "memory_context": [],
        "tool_results": [],
        "citations": [],
        "answer": "",
        "errors": [],
        "warnings": [],
        "attempts": 0,
        "status": "running",
    }
    result = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t-1"}})
    assert result["routes"] == [Route.SQL.value, Route.GRAPH.value, Route.RAG.value]
    assert len(result["tool_results"]) == 3
    assert {citation["source_type"] for citation in result["citations"]} == {
        "sql",
        "graph",
        "document",
    }
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_no_scope_is_retried_then_failed() -> None:
    graph = build_sales_graph(MockToolGateway(), InMemoryMemoryService(), max_tool_iterations=2)
    initial = {
        "request_id": "req-2",
        "session_id": "session-2",
        "user_id": "sales-2",
        "tenant_id": "tenant-1",
        "roles": ["sales"],
        "scope_tags": [],
        "query": "统计销售额",
        "locale": "zh-CN",
        "routes": [],
        "memory_context": [],
        "tool_results": [],
        "citations": [],
        "answer": "",
        "errors": [],
        "warnings": [],
        "attempts": 0,
        "status": "running",
    }
    result = await graph.ainvoke(initial, config={"configurable": {"thread_id": "t-2"}})
    assert result["attempts"] == 2
    assert result["status"] == "failed"


def test_checkpoint_state_is_safe_data_only() -> None:
    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    state = {
        "routes": ["sql"],
        "tool_results": [
            {
                "tool_name": "sales_metrics",
                "route": "sql",
                "data": [{"revenue": 1}],
                "citations": [],
            }
        ],
        "citations": [],
    }
    kind, payload = serializer.dumps_typed(state)
    restored = serializer.loads_typed((kind, payload))
    assert restored == state


class FakeLLM:
    async def route(self, _: str) -> tuple[list[Route], dict[str, int]]:
        return [Route.SQL], {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}

    async def synthesize(self, *_: object) -> LLMGeneration:
        return LLMGeneration(
            text="模型生成的有据结论（sales_metrics:req-llm）",
            model="fake-model",
            usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
        )


@pytest.mark.asyncio
async def test_graph_can_use_pluggable_llm() -> None:
    graph = build_sales_graph(
        MockToolGateway(),
        InMemoryMemoryService(),
        llm=FakeLLM(),  # type: ignore[arg-type]
    )
    initial = {
        "request_id": "req-llm",
        "session_id": "session-llm",
        "user_id": "sales-1",
        "tenant_id": "tenant-1",
        "roles": ["sales"],
        "scope_tags": ["sales:sales-1"],
        "query": "分析本期业务",
        "locale": "zh-CN",
        "routes": [],
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
    result = await graph.ainvoke(initial, config={"configurable": {"thread_id": "thread-llm"}})
    assert result["routes"] == ["sql"]
    assert result["answer"].startswith("模型生成")
    assert result["llm_usage"]["total_tokens"] == 19


class EmptyRagGateway(MockToolGateway):
    async def search_documents(self, principal, params: RagQueryParams, request_id):
        del principal, params, request_id
        return ToolResult(
            tool_name="search_documents",
            route=Route.RAG,
            data=[],
            confidence=0.0,
        )


@pytest.mark.asyncio
async def test_empty_rag_result_explicitly_refuses_unsupported_answer() -> None:
    graph = build_sales_graph(EmptyRagGateway(), InMemoryMemoryService())
    initial = {
        "request_id": "req-empty-rag",
        "session_id": "session-empty-rag",
        "user_id": "sales-1",
        "tenant_id": "tenant-1",
        "roles": ["sales"],
        "scope_tags": ["region:east"],
        "query": "查找火星基地维护纪要",
        "locale": "zh-CN",
        "routes": [],
        "memory_context": [],
        "tool_results": [],
        "citations": [],
        "answer": "",
        "errors": [],
        "warnings": [],
        "attempts": 0,
        "status": "running",
    }
    result = await graph.ainvoke(
        initial, config={"configurable": {"thread_id": "thread-empty-rag"}}
    )
    assert "暂不根据文档作答" in result["answer"]
    assert result["status"] == "needs_human_review"

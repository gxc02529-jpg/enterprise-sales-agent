import json

import httpx
import pytest

from sales_agent.config import Settings
from sales_agent.contracts import Route
from sales_agent.llm.provider import OpenAICompatibleLLMProvider


def _client(response_content: str) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        assert payload["model"] == "private-model"
        return httpx.Response(
            200,
            json={
                "model": "private-model",
                "choices": [{"message": {"role": "assistant", "content": response_content}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        )

    return httpx.AsyncClient(
        base_url="http://llm.local/v1/", transport=httpx.MockTransport(handler)
    )


def _settings() -> Settings:
    return Settings(
        llm_base_url="http://llm.local/v1",
        llm_api_key="secret-key",
        llm_model="private-model",
        llm_reasoning_backend="llm",
    )


@pytest.mark.asyncio
async def test_openai_compatible_router() -> None:
    async with _client('{"routes":["sql","rag"]}') as client:
        provider = OpenAICompatibleLLMProvider(_settings(), client=client)
        routes, usage = await provider.route("统计销售额并查看纪要")
    assert routes == [Route.SQL, Route.RAG]
    assert usage["total_tokens"] == 16


@pytest.mark.asyncio
async def test_grounded_synthesis() -> None:
    async with _client("结论来自工具证据（sql:001）") as client:
        provider = OpenAICompatibleLLMProvider(_settings(), client=client)
        result = await provider.synthesize(
            "销售额是多少",
            [{"data": {"revenue": 100}, "citations": [{"source_id": "sql:001"}]}],
            [],
        )
    assert "sql:001" in result.text
    assert result.model == "private-model"


@pytest.mark.asyncio
async def test_router_falls_back_when_gateway_has_no_json_mode() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(
            200,
            json={
                "model": "private-model",
                "choices": [{"message": {"content": '{"routes":["graph"]}'}}],
                "usage": {},
            },
        )

    async with httpx.AsyncClient(
        base_url="http://llm.local/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleLLMProvider(_settings(), client=client)
        routes, _ = await provider.route("客户关系")
    assert routes == [Route.GRAPH]
    assert calls == 2

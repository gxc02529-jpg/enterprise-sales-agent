from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import httpx

from sales_agent.config import Settings
from sales_agent.contracts import LLMConfigView, LLMProbeResponse, Route
from sales_agent.prompts import PromptRegistry, build_prompt_registry
from sales_agent.resilience import ResiliencePolicy, ResilientExecutor


@dataclass(frozen=True)
class LLMGeneration:
    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(ABC):
    @abstractmethod
    async def route(
        self, query: str, *, locale: str = "zh-CN"
    ) -> tuple[list[Route], dict[str, int]]: ...

    @abstractmethod
    async def synthesize(
        self,
        query: str,
        tool_results: list[dict[str, Any]],
        memory_context: list[dict[str, Any]],
        *,
        locale: str = "zh-CN",
    ) -> LLMGeneration: ...

    @abstractmethod
    async def probe(self) -> LLMProbeResponse: ...

    @abstractmethod
    async def close(self) -> None: ...


def config_view(settings: Settings) -> LLMConfigView:
    return LLMConfigView(
        provider=settings.llm_provider,
        api_style=settings.llm_api_style,
        reasoning_backend=settings.llm_reasoning_backend,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        router_model=settings.llm_router_model or settings.llm_model,
        synthesis_model=settings.llm_synthesis_model or settings.llm_model,
        api_key_configured=bool(settings.llm_api_key.get_secret_value()),
        timeout_seconds=settings.llm_timeout_seconds,
        connect_timeout_seconds=settings.llm_connect_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
        max_retries=settings.llm_max_retries,
        circuit_failure_threshold=settings.llm_circuit_failure_threshold,
        circuit_recovery_seconds=settings.llm_circuit_recovery_seconds,
        max_concurrency=settings.llm_max_concurrency,
        intent_router_backend=settings.intent_router_backend,
        intent_confidence_threshold=settings.intent_confidence_threshold,
        laya_model=settings.laya_model,
        laya_device=settings.laya_device,
        laya_preload=settings.laya_preload,
    )


class OpenAICompatibleLLMProvider(LLMProvider):
    """Chat Completions adapter for OpenAI, DeepSeek, Qwen and private gateways."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(
                settings.llm_timeout_seconds,
                connect=settings.llm_connect_timeout_seconds,
            ),
        )
        self.prompts = prompts or build_prompt_registry(settings)
        self.executor = ResilientExecutor(
            "llm:chat_completions",
            ResiliencePolicy(
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
                failure_threshold=settings.llm_circuit_failure_threshold,
                recovery_timeout_seconds=settings.llm_circuit_recovery_seconds,
                max_concurrency=settings.llm_max_concurrency,
                bulkhead_wait_seconds=settings.llm_bulkhead_wait_seconds,
            ),
        )

    async def _completion(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        model: str | None = None,
    ) -> LLMGeneration:
        selected_model = model or self.settings.llm_model
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": (self.settings.llm_temperature if temperature is None else temperature),
            "max_tokens": max_tokens or self.settings.llm_max_output_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async def request_once() -> LLMGeneration:
            response = await self.client.post("chat/completions", json=payload)
            if response.status_code in {400, 422} and "response_format" in payload:
                # Some OpenAI-compatible private gateways do not implement JSON mode.
                compatible_payload = {k: v for k, v in payload.items() if k != "response_format"}
                response = await self.client.post("chat/completions", json=compatible_payload)
            response.raise_for_status()
            body = response.json()
            text = body["choices"][0]["message"]["content"] or ""
            raw_usage = body.get("usage", {})
            usage = {
                "input_tokens": int(raw_usage.get("prompt_tokens", 0)),
                "output_tokens": int(raw_usage.get("completion_tokens", 0)),
                "total_tokens": int(raw_usage.get("total_tokens", 0)),
            }
            return LLMGeneration(
                text=text,
                model=str(body.get("model", selected_model)),
                usage=usage,
            )

        return await self.executor.run(request_once)

    @staticmethod
    def _json_object(text: str) -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError("LLM JSON response must be an object")
        return payload

    async def route(
        self, query: str, *, locale: str = "zh-CN"
    ) -> tuple[list[Route], dict[str, int]]:
        system_prompt = self.prompts.render("intent.route.system", locale)
        generation = await self._completion(
            [
                {
                    "role": "system",
                    "content": system_prompt.content,
                },
                {"role": "user", "content": query},
            ],
            max_tokens=120,
            temperature=0,
            json_mode=True,
            model=self.settings.llm_router_model or self.settings.llm_model,
        )
        payload = self._json_object(generation.text)
        raw_routes = payload.get("routes", [])
        routes = list(dict.fromkeys(Route(str(item)) for item in raw_routes))
        if not routes:
            raise ValueError("LLM router returned no valid route")
        if routes == [Route.EXPORT]:
            routes.insert(0, Route.SQL)
        return routes, generation.usage

    async def synthesize(
        self,
        query: str,
        tool_results: list[dict[str, Any]],
        memory_context: list[dict[str, Any]],
        *,
        locale: str = "zh-CN",
    ) -> LLMGeneration:
        evidence = {
            "tool_results": tool_results,
            "memory_context": memory_context,
        }
        system_prompt = self.prompts.render("answer.synthesis.system", locale)
        user_prompt = self.prompts.render(
            "answer.synthesis.user",
            locale,
            query=query,
            evidence=json.dumps(evidence, ensure_ascii=False, default=str),
        )
        return await self._completion(
            [
                {
                    "role": "system",
                    "content": system_prompt.content,
                },
                {
                    "role": "user",
                    "content": user_prompt.content,
                },
            ],
            model=self.settings.llm_synthesis_model or self.settings.llm_model,
        )

    async def probe(self) -> LLMProbeResponse:
        if not self.settings.llm_api_key.get_secret_value():
            return LLMProbeResponse(
                status="not_configured",
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                detail="LLM_API_KEY is not configured",
            )
        started = perf_counter()
        try:
            result = await self._completion(
                [
                    {"role": "system", "content": "仅返回 OK"},
                    {"role": "user", "content": "health check"},
                ],
                max_tokens=8,
                temperature=0,
            )
            status = "ok" if result.text.strip() else "unavailable"
            return LLMProbeResponse(
                status=status,
                provider=self.settings.llm_provider,
                model=result.model,
                elapsed_ms=int((perf_counter() - started) * 1000),
            )
        except Exception as exc:
            return LLMProbeResponse(
                status="unavailable",
                provider=self.settings.llm_provider,
                model=self.settings.llm_model,
                elapsed_ms=int((perf_counter() - started) * 1000),
                detail=type(exc).__name__,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def resilience_snapshot(self) -> dict[str, object]:
        return self.executor.snapshot()


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    if not settings.llm_api_key.get_secret_value():
        return None
    return OpenAICompatibleLLMProvider(settings)

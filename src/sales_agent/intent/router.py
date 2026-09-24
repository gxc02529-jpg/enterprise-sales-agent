from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sales_agent.agent.router import (
    EXPORT_TERMS,
    GRAPH_TERMS,
    RAG_TERMS,
    SQL_TERMS,
    route_query,
)
from sales_agent.config import Settings
from sales_agent.contracts import Route
from sales_agent.llm.provider import LLMProvider


@dataclass(frozen=True)
class RoutingDecision:
    routes: list[Route]
    confidence: float
    backend: str
    usage: dict[str, int] | None = None


class IntentRouter(ABC):
    @abstractmethod
    async def route(self, query: str, *, locale: str) -> RoutingDecision: ...


class RuleIntentRouter(IntentRouter):
    async def route(self, query: str, *, locale: str) -> RoutingDecision:
        del locale
        normalized = query.lower()
        matched = any(
            term in normalized
            for terms in (SQL_TERMS, GRAPH_TERMS, RAG_TERMS, EXPORT_TERMS)
            for term in terms
        )
        return RoutingDecision(
            routes=route_query(query),
            confidence=1.0 if matched else 0.45,
            backend="rules",
        )


class LLMIntentRouter(IntentRouter):
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    async def route(self, query: str, *, locale: str) -> RoutingDecision:
        routes, usage = await self.llm.route(query, locale=locale)
        return RoutingDecision(routes=routes, confidence=0.8, backend="llm", usage=usage)


class LayaIntentRouter(IntentRouter):
    """Optional local System-1 router. Model loading is lazy and off the event loop."""

    QUESTIONS = {
        "sql": {
            "type": "noul",
            "instructions": (
                "Does this request require sales metrics, aggregation, ranking or trends?"
            ),
        },
        "graph": {
            "type": "noul",
            "instructions": (
                "Does this request require entity relations or multi-hop graph traversal?"
            ),
        },
        "rag": {
            "type": "noul",
            "instructions": (
                "Does this request ask about documents, meeting notes or textual evidence?"
            ),
        },
        "export": {
            "type": "noul",
            "instructions": "Does this request explicitly ask to export or download a report?",
        },
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._router: Any | None = None
        self._lock = asyncio.Lock()

    async def _get_router(self) -> Any:
        if self._router is not None:
            return self._router
        async with self._lock:
            if self._router is None:
                try:
                    from laya import Router
                except ImportError as exc:
                    raise RuntimeError(
                        "Laya routing requires the optional dependency: pip install '.[laya]'"
                    ) from exc
                self._router = await asyncio.to_thread(
                    Router,
                    preload=self.settings.laya_preload,
                    device=self.settings.laya_device,
                )
        return self._router

    async def route(self, query: str, *, locale: str) -> RoutingDecision:
        router = await self._get_router()
        model = None if self.settings.laya_model == "auto" else self.settings.laya_model

        def predict() -> dict[str, Any]:
            kwargs = {"model": model} if model else {}
            return router.predict(
                {"text": query, "locale": locale},
                self.QUESTIONS,
                **kwargs,
            )

        result = await asyncio.to_thread(predict)
        answers = result.get("answers", {})
        probabilities = {
            route: float((answers.get(route.value) or {}).get("noul", 0.0))
            for route in Route
        }
        routes = [
            route
            for route, probability in probabilities.items()
            if probability >= self.settings.intent_confidence_threshold
        ]
        if routes == [Route.EXPORT]:
            routes.insert(0, Route.SQL)
        confidence = min((probabilities[route] for route in routes), default=0.0)
        return RoutingDecision(
            routes=routes,
            confidence=confidence,
            backend=f"laya:{result.get('routing', {}).get('model', 'unknown')}",
        )


def build_intent_router(
    settings: Settings, llm: LLMProvider | None
) -> IntentRouter:
    if settings.intent_router_backend == "laya":
        return LayaIntentRouter(settings)
    if settings.intent_router_backend == "llm" and llm is not None:
        return LLMIntentRouter(llm)
    return RuleIntentRouter()

import sys
from types import ModuleType

import pytest

from sales_agent.config import Settings
from sales_agent.contracts import Route
from sales_agent.intent.router import LayaIntentRouter


class _FakeLayaRouter:
    def __init__(self, **_: object) -> None:
        pass

    def predict(self, state, questions, **kwargs):
        del state, questions, kwargs
        return {
            "answers": {
                "sql": {"noul": 0.92},
                "graph": {"noul": 0.12},
                "rag": {"noul": 0.81},
                "export": {"noul": 0.05},
            },
            "routing": {"model": "multilingual"},
        }


@pytest.mark.asyncio
async def test_laya_router_maps_multi_label_answers(monkeypatch) -> None:
    fake_module = ModuleType("laya")
    fake_module.Router = _FakeLayaRouter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "laya", fake_module)
    router = LayaIntentRouter(Settings(intent_confidence_threshold=0.65))
    decision = await router.route("统计销售额并查纪要", locale="zh-CN")
    assert decision.routes == [Route.SQL, Route.RAG]
    assert decision.confidence == pytest.approx(0.81)
    assert decision.backend == "laya:multilingual"

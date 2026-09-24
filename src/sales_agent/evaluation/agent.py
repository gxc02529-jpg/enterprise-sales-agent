from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from pydantic import BaseModel, Field

from sales_agent.agent.graph import build_sales_graph
from sales_agent.contracts import (
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
)
from sales_agent.evaluation.rag import load_jsonl
from sales_agent.intent.router import RuleIntentRouter
from sales_agent.memory import InMemoryMemoryService
from sales_agent.tools.mock import MockToolGateway


class AgentEvalCase(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    level: int = Field(ge=1, le=3)
    query: str = Field(min_length=1, max_length=8_000)
    principal: Principal
    expected_routes: list[Route]
    expected_status: str
    expected_tool_names: list[str] = Field(default_factory=list)
    expected_tool_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required_citation_types: list[str] = Field(default_factory=list)
    forbidden_tool_names: list[str] = Field(default_factory=list)
    forbidden_citation_types: list[str] = Field(default_factory=list)
    forbidden_answer_fragments: list[str] = Field(default_factory=list)
    expect_clarification: bool = False
    expected_clarification_fields: list[str] = Field(default_factory=list)
    clarification_answers: dict[str, str | list[str]] | None = None
    max_attempts: int | None = None
    gateway_scenario: str = "normal"


class AgentCaseResult(BaseModel):
    case_id: str
    level: int
    passed: bool
    route_exact: bool
    tool_exact: bool
    tool_arguments_correct: bool
    tool_call_count: int
    status_correct: bool
    citation_coverage: float
    clarification_correct: bool
    safety_violations: list[str]
    actual_routes: list[str]
    actual_tools: list[str]
    actual_status: str
    clarification_observed: bool
    attempts: int
    elapsed_ms: int


class AgentEvalReport(BaseModel):
    benchmark: str = "sales-agent-gaia-style"
    backend: str
    total_cases: int
    task_success_rate: float
    route_exact_match: float
    route_micro_f1: float
    tool_exact_match: float
    tool_argument_accuracy: float
    status_accuracy: float
    citation_coverage: float
    clarification_accuracy: float
    safety_violation_rate: float
    average_latency_ms: float
    p95_latency_ms: int
    success_by_level: dict[str, float]
    passed: bool
    thresholds: dict[str, float]
    cases: list[AgentCaseResult]


def _initial_state(case: AgentEvalCase) -> dict[str, Any]:
    return {
        "request_id": f"eval:{case.case_id}:{uuid4()}",
        "session_id": f"eval:{case.case_id}",
        "user_id": case.principal.user_id,
        "tenant_id": case.principal.tenant_id,
        "roles": case.principal.roles,
        "scope_tags": case.principal.scope_tags,
        "query": case.query,
        "locale": "zh-CN",
        "routes": [],
        "routing_confidence": 0.0,
        "routing_backend": "unresolved",
        "resolved_entity_name": "",
        "clarification": None,
        "clarification_answers": {},
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


class RecordingMockToolGateway(MockToolGateway):
    def __init__(self, scenario: str = "normal") -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.scenario = scenario
        self._sales_failures = 0

    async def sales_metrics(self, principal, params: SalesMetricParams, request_id):
        self.calls.append(("sales_metrics", params.model_dump(mode="json")))
        if self.scenario == "transient_sql_once" and self._sales_failures == 0:
            self._sales_failures += 1
            raise TimeoutError("injected transient SQL timeout")
        return await super().sales_metrics(principal, params, request_id)

    async def graph_relations(self, principal, params: GraphQueryParams, request_id):
        self.calls.append(("graph_relations", params.model_dump(mode="json")))
        return await super().graph_relations(principal, params, request_id)

    async def search_documents(self, principal, params: RagQueryParams, request_id):
        self.calls.append(("search_documents", params.model_dump(mode="json")))
        return await super().search_documents(principal, params, request_id)

    async def export_report(self, principal, payload, request_id):
        self.calls.append(("export_report", dict(payload)))
        return await super().export_report(principal, payload, request_id)


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return actual == expected
    return actual == expected


def _f1_counts(expected: set[str], actual: set[str]) -> tuple[int, int, int]:
    return len(expected & actual), len(actual - expected), len(expected - actual)


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def evaluate_agent(
    cases: list[AgentEvalCase],
    *,
    min_task_success: float = 0.9,
    min_route_exact: float = 0.95,
    min_tool_exact: float = 0.95,
    min_tool_argument_accuracy: float = 0.95,
    min_citation_coverage: float = 1.0,
    max_safety_violation_rate: float = 0.0,
) -> AgentEvalReport:
    results: list[AgentCaseResult] = []
    route_tp = route_fp = route_fn = 0
    latencies: list[int] = []

    for case in cases:
        gateway = RecordingMockToolGateway(case.gateway_scenario)
        graph = build_sales_graph(
            gateway,
            InMemoryMemoryService(),
            intent_router=RuleIntentRouter(),
        )
        started = perf_counter()
        config = {"configurable": {"thread_id": f"agent-eval:{case.case_id}:{uuid4()}"}}
        state = await graph.ainvoke(_initial_state(case), config=config)
        clarification_observed = state.get("status") == "needs_clarification"
        clarification_fields = set(
            (state.get("clarification") or {}).get("required_fields", [])
        )
        clarification_correct = (
            clarification_observed == case.expect_clarification
            and set(case.expected_clarification_fields).issubset(clarification_fields)
        )
        if clarification_observed and case.clarification_answers is not None:
            state = await graph.ainvoke(
                Command(resume=case.clarification_answers), config=config
            )

        elapsed_ms = int((perf_counter() - started) * 1_000)
        latencies.append(elapsed_ms)
        actual_routes = [str(item) for item in state.get("routes", [])]
        actual_tools = list(dict.fromkeys(name for name, _ in gateway.calls))
        citation_types = {
            str(item.get("source_type"))
            for item in state.get("citations", [])
            if isinstance(item, dict) and item.get("source_type")
        }
        expected_routes = {route.value for route in case.expected_routes}
        actual_route_set = set(actual_routes)
        tp, fp, fn = _f1_counts(expected_routes, actual_route_set)
        route_tp += tp
        route_fp += fp
        route_fn += fn
        route_exact = actual_route_set == expected_routes
        tool_exact = set(actual_tools) == set(case.expected_tool_names)
        tool_arguments_correct = all(
            any(
                name == expected_name and _contains(arguments, expected_arguments)
                for name, arguments in gateway.calls
            )
            for expected_name, expected_arguments in case.expected_tool_arguments.items()
        )
        status_correct = state.get("status") == case.expected_status
        required_citations = set(case.required_citation_types)
        citation_coverage = (
            len(required_citations & citation_types) / len(required_citations)
            if required_citations
            else 1.0
        )
        violations: list[str] = []
        forbidden_tools = set(case.forbidden_tool_names) & set(actual_tools)
        if forbidden_tools:
            violations.append(f"forbidden_tools:{','.join(sorted(forbidden_tools))}")
        forbidden_citations = set(case.forbidden_citation_types) & citation_types
        if forbidden_citations:
            violations.append(
                f"forbidden_citations:{','.join(sorted(forbidden_citations))}"
            )
        answer = str(state.get("answer", ""))
        for fragment in case.forbidden_answer_fragments:
            if fragment in answer:
                violations.append(f"forbidden_answer_fragment:{fragment}")
        attempts = int(state.get("attempts", 0))
        attempts_correct = case.max_attempts is None or attempts <= case.max_attempts
        passed = all(
            (
                route_exact,
                tool_exact,
                tool_arguments_correct,
                status_correct,
                citation_coverage == 1.0,
                clarification_correct,
                not violations,
                attempts_correct,
            )
        )
        results.append(
            AgentCaseResult(
                case_id=case.case_id,
                level=case.level,
                passed=passed,
                route_exact=route_exact,
                tool_exact=tool_exact,
                tool_arguments_correct=tool_arguments_correct,
                tool_call_count=len(gateway.calls),
                status_correct=status_correct,
                citation_coverage=citation_coverage,
                clarification_correct=clarification_correct,
                safety_violations=violations,
                actual_routes=actual_routes,
                actual_tools=actual_tools,
                actual_status=str(state.get("status", "unknown")),
                clarification_observed=clarification_observed,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
            )
        )

    total = len(results)
    task_success = sum(item.passed for item in results) / total if total else 0.0
    route_exact_score = sum(item.route_exact for item in results) / total if total else 0.0
    tool_exact_score = sum(item.tool_exact for item in results) / total if total else 0.0
    tool_argument_score = (
        sum(item.tool_arguments_correct for item in results) / total if total else 0.0
    )
    status_accuracy = sum(item.status_correct for item in results) / total if total else 0.0
    citation_score = (
        sum(item.citation_coverage for item in results) / total if total else 0.0
    )
    clarification_score = (
        sum(item.clarification_correct for item in results) / total if total else 0.0
    )
    safety_rate = (
        sum(bool(item.safety_violations) for item in results) / total if total else 0.0
    )
    route_f1 = (
        2 * route_tp / (2 * route_tp + route_fp + route_fn)
        if route_tp or route_fp or route_fn
        else 0.0
    )
    by_level = {
        f"level_{level}": (
            sum(item.passed for item in results if item.level == level)
            / sum(1 for item in results if item.level == level)
        )
        for level in (1, 2, 3)
        if any(item.level == level for item in results)
    }
    thresholds = {
        "min_task_success": min_task_success,
        "min_route_exact": min_route_exact,
        "min_tool_exact": min_tool_exact,
        "min_tool_argument_accuracy": min_tool_argument_accuracy,
        "min_citation_coverage": min_citation_coverage,
        "max_safety_violation_rate": max_safety_violation_rate,
    }
    passed = (
        bool(results)
        and task_success >= min_task_success
        and route_exact_score >= min_route_exact
        and tool_exact_score >= min_tool_exact
        and tool_argument_score >= min_tool_argument_accuracy
        and citation_score >= min_citation_coverage
        and safety_rate <= max_safety_violation_rate
    )
    return AgentEvalReport(
        backend="mock/rules",
        total_cases=total,
        task_success_rate=task_success,
        route_exact_match=route_exact_score,
        route_micro_f1=route_f1,
        tool_exact_match=tool_exact_score,
        tool_argument_accuracy=tool_argument_score,
        status_accuracy=status_accuracy,
        citation_coverage=citation_score,
        clarification_accuracy=clarification_score,
        safety_violation_rate=safety_rate,
        average_latency_ms=(sum(latencies) / len(latencies) if latencies else 0.0),
        p95_latency_ms=_percentile(latencies, 0.95),
        success_by_level=by_level,
        passed=passed,
        thresholds=thresholds,
        cases=results,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run GAIA-style sales agent evaluation")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-task-success", type=float, default=0.9)
    parser.add_argument("--min-route-exact", type=float, default=0.95)
    parser.add_argument("--min-tool-exact", type=float, default=0.95)
    parser.add_argument("--min-tool-argument-accuracy", type=float, default=0.95)
    parser.add_argument("--min-citation-coverage", type=float, default=1.0)
    parser.add_argument("--max-safety-violation-rate", type=float, default=0.0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    report = await evaluate_agent(
        load_jsonl(args.dataset, AgentEvalCase),
        min_task_success=args.min_task_success,
        min_route_exact=args.min_route_exact,
        min_tool_exact=args.min_tool_exact,
        min_tool_argument_accuracy=args.min_tool_argument_accuracy,
        min_citation_coverage=args.min_citation_coverage,
        max_safety_violation_rate=args.max_safety_violation_rate,
    )
    payload = report.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report.passed else 1


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()

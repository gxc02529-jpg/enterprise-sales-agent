"""Offline diagnostic probes, deliberately not a business acceptance scorer."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from sales_agent.agent.graph import build_sales_graph
from sales_agent.contracts import Principal
from sales_agent.evaluation.agent import (
    AgentEvalCase,
    RecordingMockToolGateway,
    _initial_state,
    _percentile,
)
from sales_agent.intent.router import RuleIntentRouter
from sales_agent.memory import InMemoryMemoryService

SINGLE_TURN = {
    "sql_basic": "sql",
    "sql_advanced": "sql",
    "graph": "graph",
    "rag": "rag",
}


async def probe_case(case: dict) -> dict:
    router = RuleIntentRouter()
    decision = await router.route(case["query"], locale="zh-CN")
    result = {
        "case_id": case["case_id"],
        "category": case["category"],
        "query": case["query"],
        "acceptance_checks": case["acceptance_checks"],
        "observed_routes": [route.value for route in decision.routes],
        "routing_confidence": decision.confidence,
        "graph_executed": False,
        "business_acceptance": "not_evaluated",
        "business_success": None,
        "calls": [],
        "elapsed_ms": None,
    }
    target = SINGLE_TURN.get(case["category"])
    if target is None:
        result["blocked_reason"] = (
            "Only text routing probed; required scenario fixture, oracle or turn script unbound."
        )
        return result
    # This coarse category label is diagnostic only, not a reviewed per-case tool oracle.
    result["category_route_hint"] = target
    result["category_route_only_match"] = result["observed_routes"] == [target]
    gateway = RecordingMockToolGateway()
    graph = build_sales_graph(
        gateway,
        InMemoryMemoryService(),
        intent_router=router,
    )
    wrapper = AgentEvalCase(
        case_id=case["case_id"],
        level=case["level"],
        query=case["query"],
        principal=Principal(
            user_id="simulation-sales",
            tenant_id="simulation-tenant",
            roles=["sales"],
            scope_tags=["sales:simulation-sales"],
        ),
        expected_routes=[],
        expected_status="unused",
    )
    started = perf_counter()
    result["graph_executed"] = True
    try:
        state = await asyncio.wait_for(
            graph.ainvoke(
                _initial_state(wrapper),
                config={"configurable": {"thread_id": case["case_id"]}},
            ),
            timeout=10,
        )
        result.update(
            {
                "status": state.get("status"),
                "answer": state.get("answer"),
                "clarification": state.get("clarification"),
                "warnings": state.get("warnings", []),
                "errors": state.get("errors", []),
                "llm_usage": state.get("llm_usage", {}),
                "citations": state.get("citations", []),
            }
        )
    except Exception as exc:
        result.update(status="probe_error", error=f"{type(exc).__name__}: {exc}")
    result["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    result["calls"] = [{"name": name, "arguments": args} for name, args in gateway.calls]
    # These are raw observations, never a full parameter-correctness score.
    if target == "sql":
        sql_args = [args for name, args in gateway.calls if name == "sales_metrics"]
        result["sql_call_observed"] = bool(sql_args)
        result["sql_date_bounds_present"] = (
            all(args.get("start_date") and args.get("end_date") for args in sql_args)
            if sql_args
            else None
        )
    return result


def summarize(results: list[dict]) -> dict:
    executed = [row for row in results if row["graph_executed"]]
    times = [row["elapsed_ms"] for row in executed]
    sql = [row for row in executed if row.get("sql_call_observed")]
    categories = {}
    for category in SINGLE_TURN:
        subset = [row for row in executed if row["category"] == category]
        categories[category] = {
            "executed": len(subset),
            "category_route_only_matches": sum(row["category_route_only_match"] for row in subset),
            "statuses": dict(Counter(row["status"] for row in subset)),
        }
    return {
        "total_questions": len(results),
        "routing_text_probes": len(results),
        "single_turn_graph_probes": len(executed),
        "scenario_execution_blocked": len(results) - len(executed),
        "business_acceptance_evaluated": 0,
        "business_task_success_rate": None,
        "tool_accuracy": None,
        "parameter_accuracy": None,
        "statuses": dict(Counter(row["status"] for row in executed)),
        "category_diagnostics": categories,
        "sql_tasks_with_sql_call": len(sql),
        "sql_tasks_with_date_bounds": sum(bool(row["sql_date_bounds_present"]) for row in sql),
        "in_process_mock_latency_ms": {
            "mean": round(sum(times) / len(times), 3) if times else None,
            "p50": _percentile(times, 0.50),
            "p95": _percentile(times, 0.95),
            "p99": _percentile(times, 0.99),
        },
        "llm_calls": 0,
        "billed_tokens": 0,
        "llm_api_cost_usd": 0,
    }


def render_report(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 300题离线模拟诊断报告",
        "",
        f"生成时间：{report['created_at']}。"
        "后端：rules + MockToolGateway；未连接真实服务或LLM。",
        "",
        "## 覆盖与限制",
        "",
        f"- 路由文本探测：{summary['routing_text_probes']} / 300。",
        f"- 单轮状态机探测：{summary['single_turn_graph_probes']} / 300。",
        f"- 场景执行未覆盖：{summary['scenario_execution_blocked']} / 300。",
        "- 完整业务验收：0 / 300；业务成功率、工具准确率和参数准确率均不可计算。",
        "- 未把场景描述当作故障注入、权限变更、版本更新或多轮交互执行。",
        "- 未注入题库参考时钟，因此相对日期正确性未测。未使用真实ACL、真实数据或答案Judge。",
        "- 所有单轮调用使用独立模拟身份与内存会话；澄清不自动代答。",
        "- category_route_only_matches仅为路由与粗粒度题目分类一致的次数，不代表逐题工具正确率。",
        "- completed只是状态机结束，不代表问题答对；Mock固定数据无法验证业务数值或证据。",
        "",
        "## 按类别观察",
        "",
        "| 类别 | 单轮探测 | 仅命中类别路由 | 运行状态 |",
        "|---|---:|---:|---|",
    ]
    for category, item in summary["category_diagnostics"].items():
        lines.append(
            f"| {category} | {item['executed']} | {item['category_route_only_matches']} "
            f"| {json.dumps(item['statuses'], ensure_ascii=False)} |"
        )
    lines.extend(
        [
            "",
            "## 参数与耗时观察",
            "",
            f"SQL类中实际调用SQL的题数：{summary['sql_tasks_with_sql_call']}；"
            f"调用带起止日期的题数：{summary['sql_tasks_with_date_bounds']}。",
            "日期存在不等于日期正确；没有SQL调用的题目不进入此日期观察分母。",
            "",
            f"进程内模拟延迟（毫秒）：{summary['in_process_mock_latency_ms']}。",
            "延迟含本轮状态机运行、不含构建与前置路由探测；混合暂停和完成状态，不可外推生产P95。",
            "LLM调用0次，计费token为0，模型API费用$0；不代表部署或CPU成本为零。",
            "",
            "## 下一步",
            "",
            "1. 补齐按问题生成SQL业务参数，先验证时间、指标、维度和客户过滤。",
            "2. 为单轮题绑定独立快照和标准答案，逐题审定允许工具路径。",
            "3. 对另外180题建立真实多轮脚本、版本和故障夹具；安全项单独设置阻断门禁。",
            "4. 接入指定模型后单独测模型准确率、首字耗时、完成耗时和token费用。",
            "",
            "## 逐题记录",
            "",
            "JSON报告保留每题路由、实际调用参数、答案、暂停状态和未覆盖原因。",
            "",
        ]
    )
    for row in report["cases"]:
        lines.extend(
            [
                f"### {row['case_id']}",
                "",
                row["query"],
                "",
                f"- 路由：{row['observed_routes']}。",
                f"- 状态：{row.get('status', 'scenario_not_executed')}；业务验收：未测。",
                f"- 工具：{[call['name'] for call in row['calls']]}。",
                "",
            ]
        )
    return "\n".join(lines)


async def run(dataset: Path, output: Path) -> dict:
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    if len(cases) != 300 or len({case["case_id"] for case in cases}) != 300:
        raise ValueError("Expected 300 distinct cases")
    results = [await probe_case(case) for case in cases]
    report = {
        "schema_version": "experiment-probe-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "backend": "mock/rules/no-network",
        "dataset": str(dataset),
        "summary": summarize(results),
        "cases": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(render_report(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("evaluations/agent/experiment_300.jsonl")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("build/reports/experiment-300-probe.json")
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.dataset, args.output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

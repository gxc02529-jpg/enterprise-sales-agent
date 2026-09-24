import json
from pathlib import Path

import pytest

from sales_agent.evaluation.experiment import probe_case, summarize


def cases():
    path = Path(__file__).resolve().parents[1] / "evaluations/agent/experiment_300.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_probe_records_calls_without_claiming_business_success():
    case = next(row for row in cases() if row["case_id"] == "exp-sql_basic-03")
    result = await probe_case(case)
    assert result["graph_executed"]
    assert result["business_success"] is None
    assert result["sql_call_observed"]
    assert not result["sql_date_bounds_present"]
    summary = summarize([result])
    assert summary["business_task_success_rate"] is None
    assert summary["parameter_accuracy"] is None
    assert summary["sql_tasks_with_date_bounds"] == 0
    assert summary["billed_tokens"] == 0


@pytest.mark.asyncio
async def test_scenario_text_is_not_treated_as_fault_injection():
    case = next(row for row in cases() if row["category"] == "resilience")
    result = await probe_case(case)
    assert not result["graph_executed"]
    assert result["calls"] == []
    assert result["business_success"] is None
    summary = summarize([result])
    assert summary["scenario_execution_blocked"] == 1
    assert summary["in_process_mock_latency_ms"]["mean"] is None

from pathlib import Path

import pytest

from sales_agent.evaluation.agent import AgentEvalCase, evaluate_agent
from sales_agent.evaluation.rag import load_jsonl


@pytest.mark.asyncio
async def test_gaia_style_sales_agent_baseline_passes() -> None:
    cases = load_jsonl(Path("evaluations/agent/golden.jsonl"), AgentEvalCase)
    report = await evaluate_agent(cases)
    assert report.total_cases == 9
    assert report.task_success_rate == 1.0
    assert report.tool_argument_accuracy == 1.0
    assert report.clarification_accuracy == 1.0
    assert report.safety_violation_rate == 0.0
    assert report.passed

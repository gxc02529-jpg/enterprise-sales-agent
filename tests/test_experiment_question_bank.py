"""Check authored coverage and generated artifacts, not Agent business accuracy."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "evaluations" / "agent"
SPEC = importlib.util.spec_from_file_location(
    "experiment_question_bank", BANK / "build_experiment_300.py"
)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def test_authored_questions_have_balanced_unique_coverage():
    cases = BUILDER.build_cases()
    BUILDER.validate_cases(cases)
    assert len(cases) == 300


def test_jsonl_matches_authored_source_and_is_not_a_fake_result():
    cases = [
        json.loads(line)
        for line in (BANK / "experiment_300.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert cases == BUILDER.build_cases()
    assert all(case["readiness"] == "requires_fixture_and_oracle_review" for case in cases)
    assert all(case["result"] is None for case in cases)
    assert all(case["reference_answer"] is None for case in cases)


def test_readable_bank_matches_source_and_contains_every_case():
    cases = BUILDER.build_cases()
    rendered = (BANK / "experiment_300.md").read_text(encoding="utf-8")
    assert rendered == BUILDER.render_markdown(cases)
    for case in cases:
        assert rendered.count(f"`{case['case_id']}`") == 1

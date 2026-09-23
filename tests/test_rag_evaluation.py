from pathlib import Path

import pytest

from sales_agent.contracts import Principal, RagQueryParams, Route, ToolResult
from sales_agent.evaluation.rag import (
    RagEvalCase,
    RagEvalDocument,
    evaluate_rag,
    load_jsonl,
)


def _principal() -> Principal:
    return Principal(
        user_id="sales-1",
        tenant_id="tenant-1",
        roles=["sales"],
        scope_tags=["region:east"],
    )


@pytest.mark.asyncio
async def test_rag_metrics_cover_positive_negative_and_permissions() -> None:
    cases = [
        RagEvalCase(
            case_id="positive",
            query="positive query",
            principal=_principal(),
            expected_document_ids=["doc-expected"],
            forbidden_document_ids=["doc-secret"],
        ),
        RagEvalCase(
            case_id="negative",
            query="negative query",
            principal=_principal(),
            forbidden_document_ids=["doc-secret"],
        ),
    ]

    async def search(principal, params: RagQueryParams, request_id):
        del principal, request_id
        rows = (
            [{"document_id": "doc-expected", "score": 0.91}]
            if params.query == "positive query"
            else []
        )
        return ToolResult(tool_name="search_documents", route=Route.RAG, data=rows)

    report = await evaluate_rag(search, cases)

    assert report.passed is True
    assert report.recall_at_k == 1.0
    assert report.mrr_at_k == 1.0
    assert report.negative_rejection_rate == 1.0
    assert report.permission_violation_rate == 0.0


@pytest.mark.asyncio
async def test_permission_leak_fails_evaluation_gate() -> None:
    case = RagEvalCase(
        case_id="leak",
        query="secret",
        principal=_principal(),
        forbidden_document_ids=["doc-secret"],
    )

    async def search(principal, params, request_id):
        del principal, params, request_id
        return ToolResult(
            tool_name="search_documents",
            route=Route.RAG,
            data=[{"document_id": "doc-secret", "score": 0.99}],
        )

    report = await evaluate_rag(search, [case])

    assert report.passed is False
    assert report.permission_violation_cases == 1
    assert report.negative_rejection_rate == 0.0


@pytest.mark.asyncio
async def test_metadata_check_catches_unlisted_cross_tenant_hit() -> None:
    case = RagEvalCase(
        case_id="metadata-leak",
        query="secret",
        principal=_principal(),
    )

    async def search(principal, params, request_id):
        del principal, params, request_id
        return ToolResult(
            tool_name="search_documents",
            route=Route.RAG,
            data=[
                {
                    "document_id": "unexpected-secret",
                    "tenant_id": "other-tenant",
                    "owner_user_id": "other-sales",
                    "permission_tags": ["region:east"],
                    "customer_ids": [],
                    "document_type": "visit_note",
                    "score": 0.99,
                }
            ],
        )

    report = await evaluate_rag(search, [case])
    assert report.permission_violation_cases == 1
    assert report.cases[0].permission_violations == ["unexpected-secret"]


def test_repository_golden_dataset_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = load_jsonl(root / "evaluations" / "rag" / "golden.jsonl", RagEvalCase)
    documents = load_jsonl(
        root / "evaluations" / "rag" / "documents.jsonl", RagEvalDocument
    )
    assert len(cases) >= 5
    assert len(documents) >= 4
    assert any(not case.expected_document_ids for case in cases)
    assert all(case.forbidden_document_ids for case in cases)

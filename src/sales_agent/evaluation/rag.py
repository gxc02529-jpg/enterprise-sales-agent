from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from sales_agent.config import Settings
from sales_agent.contracts import DocumentIngestRequest, Principal, RagQueryParams, ToolResult
from sales_agent.rag.milvus import is_visible_hit
from sales_agent.tools.factory import build_data_gateway
from sales_agent.tools.gateway import ToolGateway


class RagEvalCase(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=4_000)
    principal: Principal
    expected_document_ids: list[str] = Field(default_factory=list)
    forbidden_document_ids: list[str] = Field(default_factory=list)
    customer_ids: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=20)


class RagEvalDocument(BaseModel):
    principal: Principal
    document: DocumentIngestRequest


class RagCaseResult(BaseModel):
    case_id: str
    retrieved_document_ids: list[str]
    expected_document_ids: list[str]
    forbidden_document_ids: list[str]
    recall_at_k: float | None
    reciprocal_rank: float | None
    permission_violations: list[str]
    negative_rejected: bool | None
    top_score: float | None


class RagEvalReport(BaseModel):
    total_cases: int
    positive_cases: int
    negative_cases: int
    recall_at_k: float
    mrr_at_k: float
    hit_rate_at_k: float
    negative_rejection_rate: float
    permission_violation_cases: int
    permission_violation_rate: float
    passed: bool
    thresholds: dict[str, float]
    cases: list[RagCaseResult]


SearchFunction = Callable[[Principal, RagQueryParams, str], Awaitable[ToolResult]]


def load_jsonl(path: Path, model: type[BaseModel]) -> list[Any]:
    values: list[Any] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            values.append(model.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
    return values


def _unique_document_ids(rows: Any, limit: int) -> list[str]:
    if not isinstance(rows, list):
        return []
    output: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("document_id"):
            continue
        document_id = str(row["document_id"])
        if document_id not in output:
            output.append(document_id)
        if len(output) >= limit:
            break
    return output


async def evaluate_rag(
    search: SearchFunction,
    cases: list[RagEvalCase],
    *,
    min_recall: float = 0.8,
    min_mrr: float = 0.7,
    min_negative_rejection: float = 1.0,
    max_permission_violation_rate: float = 0.0,
) -> RagEvalReport:
    results: list[RagCaseResult] = []
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    hits: list[float] = []
    negative_rejections: list[float] = []
    permission_violation_cases = 0

    for case in cases:
        params = RagQueryParams(
            query=case.query,
            customer_ids=case.customer_ids,
            document_types=case.document_types,
            top_k=case.top_k,
            rerank_top_k=case.top_k,
        )
        result = await search(case.principal, params, f"rag-eval:{case.case_id}:{uuid4()}")
        document_ids = _unique_document_ids(result.data, case.top_k)
        expected = set(case.expected_document_ids)
        forbidden = set(case.forbidden_document_ids)
        violations = set(forbidden.intersection(document_ids))
        if isinstance(result.data, list):
            for row in result.data:
                if (
                    isinstance(row, dict)
                    and row.get("tenant_id") is not None
                    and not is_visible_hit(row, case.principal, params)
                ):
                    violations.add(str(row.get("document_id") or "unknown-document"))
        sorted_violations = sorted(violations)
        permission_violation_cases += bool(sorted_violations)
        top_score = None
        if isinstance(result.data, list) and result.data:
            top_score = float(result.data[0].get("score", 0.0))

        if expected:
            recall = len(expected.intersection(document_ids)) / len(expected)
            first_rank = next(
                (index for index, item in enumerate(document_ids, 1) if item in expected),
                None,
            )
            reciprocal_rank = 1 / first_rank if first_rank is not None else 0.0
            recalls.append(recall)
            reciprocal_ranks.append(reciprocal_rank)
            hits.append(float(first_rank is not None))
            negative_rejected = None
        else:
            recall = None
            reciprocal_rank = None
            negative_rejected = not document_ids
            negative_rejections.append(float(negative_rejected))

        results.append(
            RagCaseResult(
                case_id=case.case_id,
                retrieved_document_ids=document_ids,
                expected_document_ids=case.expected_document_ids,
                forbidden_document_ids=case.forbidden_document_ids,
                recall_at_k=recall,
                reciprocal_rank=reciprocal_rank,
                permission_violations=sorted_violations,
                negative_rejected=negative_rejected,
                top_score=top_score,
            )
        )

    recall_at_k = sum(recalls) / len(recalls) if recalls else 0.0
    mrr_at_k = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    hit_rate_at_k = sum(hits) / len(hits) if hits else 0.0
    negative_rate = (
        sum(negative_rejections) / len(negative_rejections)
        if negative_rejections
        else 1.0
    )
    violation_rate = permission_violation_cases / len(cases) if cases else 0.0
    thresholds = {
        "min_recall": min_recall,
        "min_mrr": min_mrr,
        "min_negative_rejection": min_negative_rejection,
        "max_permission_violation_rate": max_permission_violation_rate,
    }
    passed = (
        bool(cases)
        and recall_at_k >= min_recall
        and mrr_at_k >= min_mrr
        and negative_rate >= min_negative_rejection
        and violation_rate <= max_permission_violation_rate
    )
    return RagEvalReport(
        total_cases=len(cases),
        positive_cases=len(recalls),
        negative_cases=len(negative_rejections),
        recall_at_k=recall_at_k,
        mrr_at_k=mrr_at_k,
        hit_rate_at_k=hit_rate_at_k,
        negative_rejection_rate=negative_rate,
        permission_violation_cases=permission_violation_cases,
        permission_violation_rate=violation_rate,
        passed=passed,
        thresholds=thresholds,
        cases=results,
    )


async def ingest_fixtures(gateway: ToolGateway, fixtures: list[RagEvalDocument]) -> None:
    for fixture in fixtures:
        await gateway.ingest_document(
            fixture.principal,
            fixture.document,
            f"rag-eval-ingest:{fixture.document.document_id}:{uuid4()}",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate permission-aware sales RAG")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--ingest-fixtures", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-mock", action="store_true")
    parser.add_argument("--min-recall", type=float, default=0.8)
    parser.add_argument("--min-mrr", type=float, default=0.7)
    parser.add_argument("--min-negative-rejection", type=float, default=1.0)
    parser.add_argument("--max-permission-violation-rate", type=float, default=0.0)
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    if settings.rag_backend != "milvus" and not args.allow_mock:
        raise RuntimeError("RAG_BACKEND=milvus is required; use --allow-mock only for smoke tests")
    gateway = build_data_gateway(settings)
    if args.ingest_fixtures:
        if args.fixtures is None:
            raise ValueError("--fixtures is required with --ingest-fixtures")
        await ingest_fixtures(gateway, load_jsonl(args.fixtures, RagEvalDocument))
    report = await evaluate_rag(
        gateway.search_documents,
        load_jsonl(args.dataset, RagEvalCase),
        min_recall=args.min_recall,
        min_mrr=args.min_mrr,
        min_negative_rejection=args.min_negative_rejection,
        max_permission_violation_rate=args.max_permission_violation_rate,
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

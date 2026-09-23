from sales_agent.contracts import DocumentIngestRequest, Principal, RagQueryParams
from sales_agent.rag.chunking import chunk_document
from sales_agent.rag.milvus import (
    build_permission_filter,
    is_visible_hit,
    select_candidates,
)


def _principal() -> Principal:
    return Principal(
        user_id='sales"001',
        tenant_id='tenant"a',
        roles=["sales"],
        scope_tags=["region:east"],
    )


def test_sales_document_chunking_is_deterministic_and_bounded() -> None:
    document = DocumentIngestRequest(
        document_id="visit-001",
        title="华东智造拜访纪要",
        document_type="visit_note",
        text=("客户预算预计第四季度确认。\n\n" * 40) + "下一步安排技术交流。",
        customer_ids=["customer-001", "customer-001"],
        permission_tags=["region:east"],
    )
    first_hash, first = chunk_document(
        _principal(), document, chunk_size_chars=200, overlap_chars=30
    )
    second_hash, second = chunk_document(
        _principal(), document, chunk_size_chars=200, overlap_chars=30
    )
    assert first_hash == second_hash
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]
    assert len(first) > 1
    assert all(item.text and len(item.text) <= 200 for item in first)
    assert all(item.customer_ids == ["customer-001"] for item in first)


def test_permission_filter_escapes_values_and_applies_all_dimensions() -> None:
    expression = build_permission_filter(
        _principal(),
        RagQueryParams(
            query="预算",
            customer_ids=['customer"001'],
            document_types=["visit_note"],
        ),
    )
    assert 'tenant_id == "tenant\\"a"' in expression
    assert 'owner_user_id == "sales\\"001"' in expression
    assert "ARRAY_CONTAINS_ANY(permission_tags" in expression
    assert "ARRAY_CONTAINS_ANY(customer_ids" in expression
    assert 'document_type in ["visit_note"]' in expression


def test_client_side_permission_check_blocks_leaked_hit() -> None:
    params = RagQueryParams(query="预算", customer_ids=["customer-001"])
    authorized = {
        "tenant_id": 'tenant"a',
        "owner_user_id": "another-user",
        "permission_tags": ["region:east"],
        "customer_ids": ["customer-001"],
        "document_type": "visit_note",
    }
    leaked = {**authorized, "tenant_id": "other-tenant"}
    assert is_visible_hit(authorized, _principal(), params)
    assert not is_visible_hit(leaked, _principal(), params)


def test_low_score_candidates_are_rejected_before_citation() -> None:
    candidates = [
        {"document_id": "weak", "score": 0.34},
        {"document_id": "strong", "score": 0.82},
    ]
    selected = select_candidates(candidates, min_score=0.35, limit=5)
    assert [item["document_id"] for item in selected] == ["strong"]

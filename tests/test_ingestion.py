import asyncio

import pytest

from sales_agent.contracts import DocumentJobMetadata, Principal
from sales_agent.rag.ingestion import InMemoryIngestionCoordinator
from sales_agent.rag.parsers import DocumentParseError, parse_document
from sales_agent.tools.mock import MockToolGateway


def _principal() -> Principal:
    return Principal(
        user_id="admin-1",
        tenant_id="tenant-1",
        roles=["admin"],
        scope_tags=[],
    )


@pytest.mark.asyncio
async def test_text_json_and_csv_parsers() -> None:
    assert await parse_document("note.txt", "拜访纪要".encode()) == "拜访纪要"
    json_text = await parse_document("note.json", b'{"budget": 100}')
    assert '"budget": 100' in json_text
    csv_text = await parse_document("contracts.csv", "合同,金额\nA,100".encode())
    assert "合同\t金额" in csv_text


@pytest.mark.asyncio
async def test_unsupported_document_fails_with_stable_code() -> None:
    with pytest.raises(DocumentParseError, match="DOCUMENT_TYPE_UNSUPPORTED"):
        await parse_document("archive.zip", b"content")


@pytest.mark.asyncio
async def test_ingestion_job_reaches_completed_state() -> None:
    coordinator = InMemoryIngestionCoordinator(
        MockToolGateway(), max_file_bytes=10_000, queue_capacity=2
    )
    await coordinator.start()
    try:
        job = await coordinator.submit(
            _principal(),
            DocumentJobMetadata(
                document_id="visit-1",
                title="客户拜访纪要",
                document_type="visit_note",
                permission_tags=["region:east"],
            ),
            filename="visit.md",
            media_type="text/markdown",
            content="客户预计第四季度确认预算。".encode(),
        )
        await asyncio.wait_for(coordinator.queue.join(), timeout=2)
        completed = await coordinator.get(_principal(), job.job_id)
        assert completed.status == "completed"
        assert completed.result is not None
        assert completed.result.chunk_count == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_ingestion_job_preserves_sanitized_failure() -> None:
    coordinator = InMemoryIngestionCoordinator(
        MockToolGateway(), max_file_bytes=10_000, queue_capacity=2
    )
    await coordinator.start()
    try:
        job = await coordinator.submit(
            _principal(),
            DocumentJobMetadata(
                document_id="bad-1", title="不支持文件", document_type="archive"
            ),
            filename="bad.zip",
            media_type="application/zip",
            content=b"not-a-document",
        )
        await asyncio.wait_for(coordinator.queue.join(), timeout=2)
        failed = await coordinator.get(_principal(), job.job_id)
        assert failed.status == "failed"
        assert failed.error_code == "DOCUMENT_TYPE_UNSUPPORTED"
        assert failed.result is None
    finally:
        await coordinator.close()

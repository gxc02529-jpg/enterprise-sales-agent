import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sales_agent.api import app, create_app
from sales_agent.config import Settings

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness() -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["backend"] == "MockToolGateway"
    assert response.json()["ingestion"]["backend"] == "memory"


def test_auth_required() -> None:
    response = client.post("/v1/analyze", json={"query": "统计销售额", "session_id": "s1"})
    assert response.status_code == 401


def test_analyze_with_dev_identity() -> None:
    response = client.post(
        "/v1/analyze",
        headers={"Authorization": "Bearer dev-token"},
        json={"query": "统计季度销售额", "session_id": "s2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["citations"][0]["source_type"] == "sql"


def test_graph_request_pauses_and_resumes_for_clarification() -> None:
    headers = {"Authorization": "Bearer dev-token"}
    initial = client.post(
        "/v1/analyze",
        headers=headers,
        json={"query": "查看客户关系", "session_id": "clarify-graph-1"},
    )
    assert initial.status_code == 200
    body = initial.json()
    assert body["status"] == "needs_clarification"
    assert body["tool_results"] == []
    assert body["clarification"]["required_fields"] == ["entity_name"]

    resumed = client.post(
        "/v1/analyze/clarify",
        headers=headers,
        json={
            "session_id": "clarify-graph-1",
            "answers": {"entity_name": "华东智造公司"},
        },
    )
    assert resumed.status_code == 200
    result = resumed.json()
    assert result["status"] == "completed"
    assert result["clarification"] is None
    assert result["tool_results"][0]["route"] == "graph"


def test_production_rejects_mock_backends() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="production",
            jwt_secret="a-production-secret",
            mcp_service_token="a-production-service-token",
            allow_dev_token=False,
            tool_backend="mcp",
            data_backend="mock",
            checkpointer_backend="postgres",
        )


def _production_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": "a-production-secret",
        "mcp_service_token": "a-production-service-token",
        "allow_dev_token": False,
        "tool_backend": "mcp",
        "data_backend": "postgres",
        "checkpointer_backend": "postgres",
        "memory_backend": "postgres",
        "audit_backend": "postgres",
        "rag_backend": "milvus",
        "api_rate_limit_backend": "redis",
        "graph_backend": "nebula",
        "export_backend": "xlsx",
        "ingestion_backend": "redis_stream",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("graph_backend", "mock", "GRAPH_BACKEND=nebula"),
        ("export_backend", "mock", "EXPORT_BACKEND=xlsx"),
        ("ingestion_backend", "memory", "INGESTION_BACKEND=redis_stream"),
    ],
)
def test_production_requires_real_graph_and_export_backends(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**_production_settings(**{field: value}))


def test_failed_payload_retention_cannot_exceed_job_retention() -> None:
    with pytest.raises(ValidationError, match="FAILED_PAYLOAD_RETENTION"):
        Settings(
            ingestion_failed_payload_retention_days=91,
            ingestion_job_retention_days=90,
        )


def test_user_memory_requires_explicit_confirmation() -> None:
    headers = {"Authorization": "Bearer dev-token"}
    proposed = client.post(
        "/v1/memories/candidates",
        headers=headers,
        json={"content": "我偏好按季度查看销售趋势"},
    )
    assert proposed.status_code == 200
    candidate = proposed.json()
    assert candidate["status"] == "candidate"

    confirmed = client.post(
        f"/v1/memories/candidates/{candidate['memory_id']}/confirm",
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "active"


def test_llm_config_is_admin_only_and_secret_free() -> None:
    forbidden = client.get(
        "/v1/admin/llm/config",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert forbidden.status_code == 403

    response = client.get(
        "/v1/admin/llm/config",
        headers={"Authorization": "Bearer dev-admin-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["api_key_configured"] is False
    assert "api_key" not in body


def test_llm_probe_reports_missing_configuration() -> None:
    response = client.post(
        "/v1/admin/llm/probe",
        headers={"Authorization": "Bearer dev-admin-token"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"


def test_configured_llm_key_is_never_returned() -> None:
    configured_app = create_app(Settings(llm_api_key="super-secret-value"))
    with TestClient(configured_app) as configured_client:
        response = configured_client.get(
            "/v1/admin/llm/config",
            headers={"Authorization": "Bearer dev-admin-token"},
        )
    assert response.status_code == 200
    assert response.json()["api_key_configured"] is True
    assert "super-secret-value" not in response.text


def test_admin_can_submit_document_ingestion() -> None:
    response = client.post(
        "/v1/admin/documents/ingest",
        headers={"Authorization": "Bearer dev-admin-token"},
        json={
            "document_id": "visit-001",
            "title": "华东智造拜访纪要",
            "document_type": "visit_note",
            "text": "客户计划第四季度确认预算。",
            "customer_ids": ["customer-001"],
            "permission_tags": ["sales:demo-sales-001"],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "simulated"
    assert response.json()["chunk_count"] == 1


def test_admin_can_retire_document_index() -> None:
    response = client.request(
        "DELETE",
        "/v1/admin/documents/visit-001",
        headers={"Authorization": "Bearer dev-admin-token"},
        json={"document_id": "visit-001", "reason": "source document was retired"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "simulated"


def test_document_path_and_body_id_must_match() -> None:
    response = client.request(
        "DELETE",
        "/v1/admin/documents/visit-001",
        headers={"Authorization": "Bearer dev-admin-token"},
        json={"document_id": "visit-002", "reason": "incorrect document"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "DOCUMENT_ID_MISMATCH"


def test_admin_can_inspect_document_version_state() -> None:
    response = client.get(
        "/v1/admin/documents/visit-001/versions",
        headers={"Authorization": "Bearer dev-admin-token"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "document_id": "visit-001",
        "status": "untracked",
        "active_version": None,
        "pending_version": None,
        "items": [],
        "count": 0,
    }


def test_sales_user_cannot_ingest_documents() -> None:
    response = client.post(
        "/v1/admin/documents/ingest",
        headers={"Authorization": "Bearer dev-token"},
        json={
            "document_id": "visit-001",
            "title": "拜访纪要",
            "document_type": "visit_note",
            "text": "受控内容",
        },
    )
    assert response.status_code == 403


def test_admin_upload_job_completes() -> None:
    job_app = create_app(Settings())
    with TestClient(job_app) as job_client:
        created = job_client.post(
            "/v1/admin/documents/jobs",
            headers={"Authorization": "Bearer dev-admin-token"},
            data={
                "document_id": "visit-upload-001",
                "title": "上传拜访纪要",
                "document_type": "visit_note",
                "permission_tags": '["sales:demo-sales-001"]',
            },
            files={"file": ("visit.md", "客户预算将在第四季度确认。", "text/markdown")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        body = created.json()
        for _ in range(50):
            current = job_client.get(
                f"/v1/admin/documents/jobs/{job_id}",
                headers={"Authorization": "Bearer dev-admin-token"},
            )
            assert current.status_code == 200
            body = current.json()
            if body["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        assert body["status"] == "completed"
        assert body["result"]["chunk_count"] == 1


def test_document_job_id_must_be_uuid() -> None:
    response = client.get(
        "/v1/admin/documents/jobs/not-a-uuid",
        headers={"Authorization": "Bearer dev-admin-token"},
    )
    assert response.status_code == 422


def test_empty_document_upload_is_rejected() -> None:
    response = client.post(
        "/v1/admin/documents/jobs",
        headers={"Authorization": "Bearer dev-admin-token"},
        data={
            "document_id": "empty-upload",
            "title": "空文件",
            "document_type": "visit_note",
        },
        files={"file": ("empty.md", b"", "text/markdown")},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "DOCUMENT_EMPTY"


def test_admin_can_list_and_retry_failed_document_job() -> None:
    job_app = create_app(Settings())
    headers = {"Authorization": "Bearer dev-admin-token"}
    with TestClient(job_app) as job_client:
        created = job_client.post(
            "/v1/admin/documents/jobs",
            headers=headers,
            data={
                "document_id": "failed-upload",
                "title": "不支持格式",
                "document_type": "visit_note",
            },
            files={"file": ("unsupported.zip", b"bad archive", "application/zip")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        body = created.json()
        for _ in range(50):
            body = job_client.get(
                f"/v1/admin/documents/jobs/{job_id}", headers=headers
            ).json()
            if body["status"] == "failed":
                break
            time.sleep(0.01)
        assert body["status"] == "failed"

        listed = job_client.get(
            "/v1/admin/documents/jobs?status=failed&limit=10", headers=headers
        )
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["items"][0]["job_id"] == job_id

        retried = job_client.post(
            f"/v1/admin/documents/jobs/{job_id}/retry", headers=headers
        )
        assert retried.status_code == 202
        assert retried.json()["status"] == "queued"


def test_sales_user_cannot_list_document_jobs() -> None:
    response = client.get(
        "/v1/admin/documents/jobs",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert response.status_code == 403

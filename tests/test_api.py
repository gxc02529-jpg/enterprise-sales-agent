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

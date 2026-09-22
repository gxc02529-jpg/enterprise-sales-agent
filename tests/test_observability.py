from fastapi.testclient import TestClient

from sales_agent.api import app
from sales_agent.observability import MetricsCollector


def test_metrics_collector_records_without_error() -> None:
    collector = MetricsCollector()
    collector.record_http("GET", "/health", 200, 0.012)
    collector.record_tool("sales_metrics", "success", 0.02)
    collector.record_tool("graph_relations", "error", 0.01)
    collector.set_circuit("tool:sales_metrics", "closed")
    rendered = collector.render()
    assert isinstance(rendered, bytes)
    assert collector.content_type.startswith("text/plain")


def test_metrics_endpoint_returns_200() -> None:
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

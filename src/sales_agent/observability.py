from __future__ import annotations

from typing import Any

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when the optional dep is absent
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"
    _PROM_AVAILABLE = False


class _NullMetric:
    def labels(self, *args: Any, **kwargs: Any) -> _NullMetric:
        return self

    def inc(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def observe(self, *args: Any, **kwargs: Any) -> None:
        return None


class MetricsCollector:
    """Prometheus metrics facade; degrades to no-ops when prometheus_client is absent.

    Each instance owns a private ``CollectorRegistry`` so repeated app construction
    (e.g. tests calling ``create_app`` multiple times) never re-registers the same
    metric name into the global registry.
    """

    def __init__(self) -> None:
        if _PROM_AVAILABLE:
            self._registry = CollectorRegistry()
            self.http_requests = Counter(
                "http_requests_total",
                "HTTP requests served",
                ["method", "route", "status"],
                registry=self._registry,
            )
            self.http_duration = Histogram(
                "http_request_duration_seconds",
                "HTTP request latency",
                ["method", "route"],
                registry=self._registry,
            )
            self.tool_calls = Counter(
                "tool_calls_total",
                "Tool invocations",
                ["tool", "outcome"],
                registry=self._registry,
            )
            self.tool_duration = Histogram(
                "tool_call_duration_seconds",
                "Tool call latency",
                ["tool"],
                registry=self._registry,
            )
            self.circuit_state = Gauge(
                "circuit_breaker_state",
                "Circuit breaker state (0=closed, 1=open, 2=half_open)",
                ["name"],
                registry=self._registry,
            )
        else:
            self._registry = None
            self.http_requests = _NullMetric()
            self.http_duration = _NullMetric()
            self.tool_calls = _NullMetric()
            self.tool_duration = _NullMetric()
            self.circuit_state = _NullMetric()

    @property
    def available(self) -> bool:
        return _PROM_AVAILABLE

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST

    def record_http(
        self, method: str, route: str, status_code: int, elapsed_seconds: float
    ) -> None:
        self.http_requests.labels(method, route, str(status_code)).inc()
        self.http_duration.labels(method, route).observe(elapsed_seconds)

    def record_tool(self, tool: str, outcome: str, elapsed_seconds: float) -> None:
        self.tool_calls.labels(tool, outcome).inc()
        self.tool_duration.labels(tool).observe(elapsed_seconds)

    def set_circuit(self, name: str, state: str) -> None:
        value = {"closed": 0, "open": 1, "half_open": 2}.get(state, 0)
        self.circuit_state.labels(name).set(value)

    def render(self) -> bytes:
        if not _PROM_AVAILABLE:
            return b"# prometheus_client is not installed; metrics are disabled\n"
        return generate_latest(self._registry)

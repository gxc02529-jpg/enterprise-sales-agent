import pytest

from sales_agent.config import Settings
from sales_agent.contracts import GraphQueryParams, Principal, SalesMetricParams
from sales_agent.tools.mock import MockToolGateway
from sales_agent.tools.nebula import (
    GraphBackendUnavailable,
    NebulaGraphGateway,
    build_graph_query,
    filter_visible_paths,
    quote_vid,
)


def _principal(roles: list[str] | None = None) -> Principal:
    return Principal(
        user_id="sales-1",
        tenant_id="demo-tenant",
        roles=roles or ["sales"],
        scope_tags=["region:east"],
    )


def test_quote_vid_escapes_quotes_and_backslashes() -> None:
    assert quote_vid('a"b\\c') == '"a\\"b\\\\c"'


def test_build_graph_query_clamps_hops_and_escapes_vid() -> None:
    params = GraphQueryParams.model_construct(entity_name='华东"智造', max_hops=9)
    ngql, ctx = build_graph_query(_principal(), params)
    assert "4 STEPS" in ngql
    assert '华东\\"智造' in ngql
    assert ctx["tenant"] == "demo-tenant"
    assert ctx["is_admin"] is False

    params_low = GraphQueryParams.model_construct(entity_name="华东智造", max_hops=0)
    ngql_low, _ = build_graph_query(_principal(), params_low)
    assert "1 STEPS" in ngql_low


def test_filter_visible_paths_drops_cross_tenant() -> None:
    paths = [
        {"edge": {"props": {"tenant_id": "demo-tenant"}}},
        {"edge": {"props": {"tenant_id": "other-tenant"}}},
        {"edge": {}},
    ]
    visible = filter_visible_paths(paths, _principal())
    assert len(visible) == 2


def test_filter_visible_paths_admin_sees_all() -> None:
    paths = [{"edge": {"props": {"tenant_id": "other-tenant"}}}]
    assert len(filter_visible_paths(paths, _principal(roles=["admin"]))) == 1


@pytest.mark.asyncio
async def test_gateway_delegates_sales_to_inner() -> None:
    gateway = NebulaGraphGateway(MockToolGateway(), Settings())
    result = await gateway.sales_metrics(
        _principal(),
        SalesMetricParams(metrics=["revenue"], dimensions=["quarter"]),
        "request-1",
    )
    assert result.tool_name == "sales_metrics"
    assert result.simulated is True


@pytest.mark.asyncio
async def test_graph_relations_raises_unavailable_without_backend() -> None:
    gateway = NebulaGraphGateway(MockToolGateway(), Settings())
    with pytest.raises(GraphBackendUnavailable):
        await gateway.graph_relations(
            _principal(), GraphQueryParams(entity_name="华东智造"), "request-2"
        )

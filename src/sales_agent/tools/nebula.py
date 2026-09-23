from __future__ import annotations

import asyncio
import time
from typing import Any

from sales_agent.config import Settings
from sales_agent.contracts import (
    Citation,
    DocumentIngestRequest,
    DocumentIngestResponse,
    GraphQueryParams,
    Principal,
    RagQueryParams,
    Route,
    SalesMetricParams,
    ToolResult,
)
from sales_agent.tools.gateway import ToolGateway


class GraphBackendUnavailable(RuntimeError):
    """Raised when the NebulaGraph backend cannot serve a graph_relations call."""


RELATION_EDGE_TYPES = {
    "RESPONSIBLE_FOR": "MANAGES",
    "SIGNED": "SIGNED",
    "CONTAINS": "CONTAINS",
    "BELONGS_TO": "BELONGS_TO",
    "BENCHMARKS": "BENCHMARKS",
}


def quote_vid(vid: str) -> str:
    """Escape a raw vertex id for safe inline use inside a double-quoted nGQL literal."""
    return '"' + vid.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_graph_query(
    principal: Principal, params: GraphQueryParams
) -> tuple[str, dict[str, Any]]:
    """Return (nGQL, filter_context).

    ``STEPS`` is a keyword that does not accept parameter binding, so ``hops`` is
    clamped to a known-safe integer and the start ``vid`` is escaped via ``quote_vid``.
    The filter context is applied client-side as defense-in-depth (NebulaGraph has no RLS).
    """
    vid = params.start_entity_id or params.entity_name or ""
    if not vid:
        raise ValueError("start_entity_id or entity_name is required")
    raw_hops = params.max_hops
    hops = max(1, min(int(raw_hops if raw_hops is not None else 2), 4))
    requested = params.relation_types or list(RELATION_EDGE_TYPES)
    edge_types = [RELATION_EDGE_TYPES[item] for item in requested]
    ngql = (
        f"GET SUBGRAPH WITH PROP {hops} STEPS FROM {quote_vid(vid)} "
        f"BOTH {','.join(edge_types)} YIELD VERTICES AS v, EDGES AS e"
    )
    filter_context = {
        "tenant": principal.tenant_id,
        "scope": set(principal.scope_tags),
        "is_admin": "admin" in principal.roles,
        "limit": params.limit,
    }
    return ngql, filter_context


def _vertex_to_dict(vertex: Any) -> dict[str, Any]:
    props: dict[str, Any] = {}
    try:
        for tag in vertex.tags():
            props[tag] = {key: vertex.get_prop(tag, key) for key in vertex.properties(tag)}
    except Exception:
        props = {}
    return {"vid": vertex.get_id().as_string(), "tags": props}


def _edge_to_dict(edge: Any) -> dict[str, Any]:
    props: dict[str, Any] = {}
    try:
        edge_name = edge.edge_name()
        props = {key: edge.get_prop(edge_name, key) for key in edge.properties(edge_name)}
    except Exception:
        props = {}
    return {
        "src": edge.start_vertex_id().as_string(),
        "dst": edge.end_vertex_id().as_string(),
        "rank": edge.ranking(),
        "type": edge.edge_name(),
        "props": props,
    }


def parse_subgraph(resp: Any) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    try:
        for i in range(resp.row_size()):
            row = resp.row_values(i)
            vertex = row[0].as_node() if hasattr(row[0], "as_node") else None
            edge = row[1].as_relationship() if hasattr(row[1], "as_relationship") else None
            paths.append(
                {
                    "vertex": _vertex_to_dict(vertex) if vertex else None,
                    "edge": _edge_to_dict(edge) if edge else None,
                }
            )
    except Exception:
        return paths
    return paths


def _path_visible(path: dict[str, Any], principal: Principal) -> bool:
    edge_props = (path.get("edge") or {}).get("props") or {}
    vertex_tags = (path.get("vertex") or {}).get("tags") or {}
    property_sets = [
        edge_props,
        *[props for props in vertex_tags.values() if isinstance(props, dict)],
    ]
    tenants = {
        str(props["tenant_id"])
        for props in property_sets
        if props.get("tenant_id") not in {None, ""}
    }
    # NebulaGraph has no RLS. Missing security metadata must fail closed.
    if not tenants or tenants != {principal.tenant_id}:
        return False
    # An admin may bypass business scope tags, but never the tenant boundary.
    if "admin" in principal.roles:
        return True
    permissions: set[str] = set()
    for props in property_sets:
        raw = props.get("permission_tags")
        if isinstance(raw, str):
            permissions.update(item.strip() for item in raw.split(",") if item.strip())
        elif isinstance(raw, list):
            permissions.update(str(item) for item in raw)
    return bool(permissions.intersection(principal.scope_tags))


def filter_visible_paths(
    paths: list[dict[str, Any]], principal: Principal, *, limit: int | None = None
) -> list[dict[str, Any]]:
    visible = [path for path in paths if _path_visible(path, principal)]
    return visible[:limit] if limit is not None else visible


class NebulaGraphGateway(ToolGateway):
    """Compose a real NebulaGraph adapter around an inner gateway.

    Only ``graph_relations`` is overridden; sales / RAG / export / ingest delegate to
    ``inner`` so a real Postgres / Milvus stack can coexist with a real graph backend.
    """

    def __init__(self, inner: ToolGateway, settings: Settings) -> None:
        self.inner = inner
        self.settings = settings
        self._pool: Any = None

    def _get_session(self) -> Any:
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        if self._pool is None:
            config = Config()
            config.max_connection_pool_size = 4
            pool = ConnectionPool()
            if not pool.init([(self.settings.nebula_host, self.settings.nebula_port)], config):
                raise GraphBackendUnavailable("无法初始化 NebulaGraph 连接池")
            self._pool = pool
        session = self._pool.get_session(self.settings.nebula_user, self.settings.nebula_password)
        result = session.execute(f"USE {self.settings.nebula_space}")
        if not result.is_succeeded():
            raise GraphBackendUnavailable(
                f"无法切换图空间 {self.settings.nebula_space}: {result.error_msg()}"
            )
        return session

    async def graph_relations(
        self, principal: Principal, params: GraphQueryParams, request_id: str
    ) -> ToolResult:
        started = time.perf_counter()
        ngql, _ = build_graph_query(principal, params)

        def _query() -> list[dict[str, Any]]:
            try:
                session = self._get_session()
            except GraphBackendUnavailable:
                raise
            except Exception as exc:
                raise GraphBackendUnavailable(f"连接 NebulaGraph 失败: {exc}") from exc
            try:
                resp = session.execute(ngql)
                if not resp.is_succeeded():
                    raise GraphBackendUnavailable(f"图查询失败: {resp.error_msg()}")
                return filter_visible_paths(parse_subgraph(resp), principal, limit=params.limit)
            finally:
                session.release()

        paths = await asyncio.to_thread(_query)
        confidence = 0.85 if paths else 0.0
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ToolResult(
            tool_name="graph_relations",
            route=Route.GRAPH,
            data=paths,
            row_count=len(paths),
            confidence=confidence,
            elapsed_ms=elapsed_ms,
            citations=(
                [
                    Citation(
                        source_type="graph",
                        source_id=(
                            f"nebula:{self.settings.nebula_space}:"
                            f"{params.start_entity_id or params.entity_name or ''}"
                        ),
                        label="客户—合同—产品关系（NebulaGraph）",
                        excerpt=f"{len(paths)} 条关系路径",
                    )
                ]
                if paths
                else []
            ),
        )

    async def sales_metrics(
        self, principal: Principal, params: SalesMetricParams, request_id: str
    ) -> ToolResult:
        return await self.inner.sales_metrics(principal, params, request_id)

    async def search_documents(
        self, principal: Principal, params: RagQueryParams, request_id: str
    ) -> ToolResult:
        return await self.inner.search_documents(principal, params, request_id)

    async def export_report(
        self, principal: Principal, payload: dict[str, Any], request_id: str
    ) -> ToolResult:
        return await self.inner.export_report(principal, payload, request_id)

    async def ingest_document(
        self, principal: Principal, document: DocumentIngestRequest, request_id: str
    ) -> DocumentIngestResponse:
        return await self.inner.ingest_document(principal, document, request_id)

    async def health(self) -> dict[str, Any]:
        base = await self.inner.health()
        return {**base, "graph_backend": "nebula", "nebula_space": self.settings.nebula_space}

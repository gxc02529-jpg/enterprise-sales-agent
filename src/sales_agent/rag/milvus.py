from __future__ import annotations

import asyncio
import json
from time import perf_counter
from typing import Any

from sales_agent.config import Settings
from sales_agent.contracts import (
    DocumentIngestRequest,
    DocumentIngestResponse,
    Principal,
    RagQueryParams,
)
from sales_agent.rag.chunking import chunk_document
from sales_agent.rag.embedding import DenseEmbedder
from sales_agent.rag.reranker import Reranker


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _array(values: list[str]) -> str:
    return json.dumps(list(dict.fromkeys(values)), ensure_ascii=False)


def build_permission_filter(principal: Principal, params: RagQueryParams) -> str:
    clauses = [f"tenant_id == {_literal(principal.tenant_id)}"]
    if "admin" not in principal.roles:
        visibility = [f"owner_user_id == {_literal(principal.user_id)}"]
        if principal.scope_tags:
            visibility.append(
                f"ARRAY_CONTAINS_ANY(permission_tags, {_array(principal.scope_tags)})"
            )
        clauses.append(f"({' or '.join(visibility)})")
    if params.customer_ids:
        clauses.append(f"ARRAY_CONTAINS_ANY(customer_ids, {_array(params.customer_ids)})")
    if params.document_types:
        clauses.append(f"document_type in {_array(params.document_types)}")
    return " and ".join(clauses)


def is_visible_hit(entity: dict[str, Any], principal: Principal, params: RagQueryParams) -> bool:
    if entity.get("tenant_id") != principal.tenant_id:
        return False
    if "admin" not in principal.roles:
        owner_visible = entity.get("owner_user_id") == principal.user_id
        tags = set(entity.get("permission_tags") or [])
        scope_visible = bool(tags.intersection(principal.scope_tags))
        if not owner_visible and not scope_visible:
            return False
    if params.customer_ids and not set(entity.get("customer_ids") or []).intersection(
        params.customer_ids
    ):
        return False
    return not params.document_types or entity.get("document_type") in params.document_types


def _hit_payload(hit: Any) -> tuple[str, float, dict[str, Any]]:
    if isinstance(hit, dict):
        entity = dict(hit.get("entity") or {})
        return str(hit.get("id", entity.get("chunk_id", ""))), float(
            hit.get("distance", hit.get("score", 0.0))
        ), entity
    entity_value = getattr(hit, "entity", {})
    entity = dict(entity_value) if entity_value is not None else {}
    return str(getattr(hit, "id", entity.get("chunk_id", ""))), float(
        getattr(hit, "distance", 0.0)
    ), entity


def select_candidates(
    candidates: list[dict[str, Any]], *, min_score: float, limit: int
) -> list[dict[str, Any]]:
    """Apply the calibrated refusal boundary after ranking and before citations."""
    return [
        item for item in candidates if float(item.get("score", 0.0)) >= min_score
    ][:limit]


class MilvusRagService:
    def __init__(
        self,
        settings: Settings,
        embedder: DenseEmbedder,
        reranker: Reranker | None,
        *,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder
        self.reranker = reranker
        self.collection_name = settings.rag_collection_name
        self._client = client
        self._client_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                try:
                    from pymilvus import MilvusClient
                except ImportError as exc:
                    raise RuntimeError("Milvus RAG requires: pip install '.[infra]'") from exc
                kwargs: dict[str, Any] = {"uri": self.settings.milvus_uri}
                token = self.settings.milvus_token.get_secret_value()
                if token:
                    kwargs["token"] = token
                self._client = await asyncio.to_thread(MilvusClient, **kwargs)
        return self._client

    async def ensure_collection(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            client = await self._get_client()
            exists = await asyncio.to_thread(client.has_collection, self.collection_name)
            if not exists:
                await asyncio.to_thread(self._create_collection, client)
            await asyncio.to_thread(client.load_collection, self.collection_name)
            self._schema_ready = True

    def _create_collection(self, client: Any) -> None:
        try:
            from pymilvus import DataType, Function, FunctionType
        except ImportError as exc:
            raise RuntimeError("Milvus RAG requires: pip install '.[infra]'") from exc
        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        for field, max_length in (
            ("document_id", 256),
            ("tenant_id", 128),
            ("owner_user_id", 128),
            ("title", 512),
            ("document_type", 64),
            ("source_uri", 2_048),
            ("version", 64),
            ("content_hash", 64),
        ):
            schema.add_field(field, DataType.VARCHAR, max_length=max_length)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field(
            "customer_ids",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=100,
            max_length=256,
        )
        schema.add_field(
            "permission_tags",
            DataType.ARRAY,
            element_type=DataType.VARCHAR,
            max_capacity=100,
            max_length=256,
        )
        schema.add_field("text", DataType.VARCHAR, max_length=20_000, enable_analyzer=True)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=self.settings.rag_embedding_dim)
        schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="text_bm25",
                input_field_names=["text"],
                output_field_names=["sparse_vector"],
                function_type=FunctionType.BM25,
            )
        )
        indexes = client.prepare_index_params()
        indexes.add_index(
            field_name="dense_vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        indexes.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
        )
        client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=indexes,
            consistency_level="Bounded",
        )

    async def ingest(
        self, principal: Principal, document: DocumentIngestRequest
    ) -> DocumentIngestResponse:
        started = perf_counter()
        content_hash, chunks = chunk_document(
            principal,
            document,
            chunk_size_chars=self.settings.rag_chunk_size_chars,
            overlap_chars=self.settings.rag_chunk_overlap_chars,
        )
        await self.ensure_collection()
        vectors = await self.embedder.embed_documents([chunk.text for chunk in chunks])
        rows = [chunk.as_milvus_row(vector) for chunk, vector in zip(chunks, vectors, strict=True)]
        client = await self._get_client()
        delete_filter = (
            f"tenant_id == {_literal(principal.tenant_id)} and "
            f"document_id == {_literal(document.document_id)}"
        )
        await asyncio.to_thread(
            client.delete, collection_name=self.collection_name, filter=delete_filter
        )
        if rows:
            await asyncio.to_thread(client.insert, self.collection_name, rows)
        elapsed_ms = int((perf_counter() - started) * 1000)
        return DocumentIngestResponse(
            document_id=document.document_id,
            content_hash=content_hash,
            chunk_count=len(rows),
            row_count=len(rows),
            status="indexed",
            elapsed_ms=elapsed_ms,
        )

    async def search(
        self, principal: Principal, params: RagQueryParams
    ) -> list[dict[str, Any]]:
        await self.ensure_collection()
        dense = await self.embedder.embed_query(params.query)
        expression = build_permission_filter(principal, params)
        candidate_limit = max(params.top_k, self.settings.rag_retrieval_top_k)
        try:
            from pymilvus import AnnSearchRequest, RRFRanker
        except ImportError as exc:
            raise RuntimeError("Milvus RAG requires: pip install '.[infra]'") from exc
        requests = [
            AnnSearchRequest(
                data=[dense],
                anns_field="dense_vector",
                param={"metric_type": "COSINE", "params": {"ef": 64}},
                limit=candidate_limit,
                expr=expression,
            ),
            AnnSearchRequest(
                data=[params.query],
                anns_field="sparse_vector",
                param={"metric_type": "BM25"},
                limit=candidate_limit,
                expr=expression,
            ),
        ]
        fields = [
            "chunk_id",
            "document_id",
            "tenant_id",
            "owner_user_id",
            "title",
            "document_type",
            "text",
            "customer_ids",
            "permission_tags",
            "source_uri",
            "version",
            "chunk_index",
            "content_hash",
        ]
        client = await self._get_client()
        raw = await asyncio.to_thread(
            client.hybrid_search,
            self.collection_name,
            requests,
            RRFRanker(),
            candidate_limit,
            output_fields=fields,
        )
        candidates: list[dict[str, Any]] = []
        for hit in raw[0] if raw else []:
            hit_id, retrieval_score, entity = _hit_payload(hit)
            entity.setdefault("chunk_id", hit_id)
            # Defense in depth: never trust datastore-side filtering alone.
            if not is_visible_hit(entity, principal, params):
                continue
            entity["retrieval_score"] = retrieval_score
            candidates.append(entity)
        if self.reranker is not None and candidates:
            scores = await self.reranker.score(params.query, [item["text"] for item in candidates])
            for item, score in zip(candidates, scores, strict=True):
                item["score"] = score
            candidates.sort(key=lambda item: item["score"], reverse=True)
        else:
            for item in candidates:
                item["score"] = item["retrieval_score"]
        min_score = (
            self.settings.rag_min_score
            if self.reranker is not None
            else self.settings.rag_min_retrieval_score
        )
        return select_candidates(
            candidates, min_score=min_score, limit=params.rerank_top_k
        )

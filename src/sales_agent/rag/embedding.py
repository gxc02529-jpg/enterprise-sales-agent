from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class DenseEmbedder(ABC):
    dimension: int

    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]: ...


class BGEM3DenseEmbedder(DenseEmbedder):
    """Lazily loads BGE-M3 so the default mock runtime stays lightweight."""

    def __init__(self, model_name: str, *, device: str, batch_size: int, dimension: int) -> None:
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self.dimension = dimension
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                try:
                    from FlagEmbedding import BGEM3FlagModel
                except ImportError as exc:
                    raise RuntimeError("BGE-M3 requires: pip install '.[rag]'") from exc
                self._model = await asyncio.to_thread(
                    BGEM3FlagModel,
                    self.model_name,
                    use_fp16=self.device != "cpu",
                    devices=self.device,
                )
        return self._model

    async def _encode(self, texts: list[str], *, queries: bool) -> list[list[float]]:
        model = await self._get_model()
        method = model.encode_queries if queries else model.encode_corpus
        result = await asyncio.to_thread(
            method,
            texts,
            batch_size=self.batch_size,
            max_length=8192,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        vectors = result["dense_vecs"]
        output = vectors.tolist() if hasattr(vectors, "tolist") else list(vectors)
        if any(len(vector) != self.dimension for vector in output):
            raise ValueError("embedding dimension does not match RAG_EMBEDDING_DIM")
        return output

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._encode(texts, queries=False)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._encode([text], queries=True))[0]

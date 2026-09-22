from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class Reranker(ABC):
    @abstractmethod
    async def score(self, query: str, documents: list[str]) -> list[float]: ...


class IdentityReranker(Reranker):
    async def score(self, query: str, documents: list[str]) -> list[float]:
        del query
        total = max(1, len(documents))
        return [1 - index / total for index in range(len(documents))]


class BGEReranker(Reranker):
    def __init__(self, model_name: str, *, device: str) -> None:
        self.model_name = model_name
        self.device = device
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                try:
                    from FlagEmbedding import FlagReranker
                except ImportError as exc:
                    raise RuntimeError("BGE reranker requires: pip install '.[rag]'") from exc
                self._model = await asyncio.to_thread(
                    FlagReranker,
                    self.model_name,
                    use_fp16=self.device != "cpu",
                    devices=self.device,
                )
        return self._model

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = await self._get_model()
        pairs = [[query, document] for document in documents]
        scores = await asyncio.to_thread(model.compute_score, pairs, normalize=True)
        if isinstance(scores, (int, float)):
            return [float(scores)]
        return [float(score) for score in scores]

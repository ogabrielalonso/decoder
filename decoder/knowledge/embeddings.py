from __future__ import annotations

import hashlib
from typing import Protocol

from decoder.utils.logging import get_logger

logger = get_logger(__name__)


class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """sentence-transformers embedder. Lazy-loads the model on first call."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model_name
        self._model = None
        self._dimensions: int | None = None

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("loading embedding model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
            self._dimensions = int(self._model.get_sentence_embedding_dimension())
        return self._model

    @property
    def dimensions(self) -> int:
        self._ensure()
        assert self._dimensions is not None
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure()
        if not texts:
            return []
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vectors]


class HashEmbedder:
    """Deterministic, zero-dependency embedder used in tests.

    Maps each token to a bucket by hash and builds a normalized bag-of-buckets
    vector. Not semantic, but stable and fast.
    """

    def __init__(self, dimensions: int = 128) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dimensions
            for token in text.lower().split():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                bucket = int.from_bytes(digest, "big") % self._dimensions
                vec[bucket] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 0:
                vec = [v / norm for v in vec]
            out.append(vec)
        return out

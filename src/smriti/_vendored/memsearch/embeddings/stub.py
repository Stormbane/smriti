"""Deterministic, dependency-free word hashing for tests, not semantic search."""

from __future__ import annotations

import hashlib
import math
import re


class StubEmbedding:
    """A normalised bag of words in 384 dimensions; empty input maps to zero.

    SHA-256 and explicit byte order keep buckets stable across processes and
    platforms. Word order is ignored, repeated words contribute their counts,
    and hash collisions are expected. This does not model semantic similarity.
    """

    def __init__(self, model: str = "stub-384", *, batch_size: int = 0) -> None:
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return 384

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimension
            for word in re.findall(r"\w+", text.casefold()):
                digest = hashlib.sha256(word.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:8], "big") % self.dimension
                vector[bucket] += 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors

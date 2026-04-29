"""Backend-agnostic recall types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RecallMatch:
    source: str
    snippet: str
    score: float


@dataclass
class RecallResponse:
    matches: list[RecallMatch]
    elapsed_ms: int
    backend: str
    error: str | None = None

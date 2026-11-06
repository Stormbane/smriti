"""Storage layer — SQLite + FTS5 + sqlite-vec index over the narada tree."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from smriti.store.schema import ensure_schema
from smriti.store.search import SearchResult, search

if TYPE_CHECKING:
    from smriti.store.indexer import index_tree

__all__ = ["ensure_schema", "index_tree", "search", "SearchResult"]


def __getattr__(name: str) -> Any:
    # The indexer (asyncio, the chunker, the embedding identity: about 140 ms
    # on Windows) is imported where it is used, not by every reader of the
    # schema: `smriti status` imports nothing else from it.
    if name == "index_tree":
        from smriti.store.indexer import index_tree

        return index_tree
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

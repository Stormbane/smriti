"""Storage layer — SQLite + FTS5 + sqlite-vec index over the narada tree."""

from smriti.store.indexer import index_tree
from smriti.store.schema import ensure_schema
from smriti.store.search import SearchResult, search

__all__ = ["ensure_schema", "index_tree", "search", "SearchResult"]

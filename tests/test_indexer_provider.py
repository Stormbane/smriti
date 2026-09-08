"""Embedding-provider selection against an existing index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from smriti.store.indexer import _provider_for_model, _recorded_provider_key


def _legacy_index(path: Path, model: str) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('model', ?)", (model,))
    conn.execute("INSERT INTO meta VALUES ('dimension', '384')")
    conn.commit()
    conn.close()
    return path


def test_legacy_index_without_provider_key_infers_from_model(tmp_path: Path) -> None:
    """Indexes from before e8d98c2 record model + dimension but no
    embedding_provider; the auto order would pick ONNX and strand a
    MiniLM index behind the dimension guard (review)."""
    db = _legacy_index(tmp_path / "index.db", "all-MiniLM-L6-v2")
    assert _recorded_provider_key(db) == "local"


def test_recorded_provider_key_wins_over_inference(tmp_path: Path) -> None:
    db = _legacy_index(tmp_path / "index.db", "all-MiniLM-L6-v2")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meta VALUES ('embedding_provider', 'onnx')")
    conn.commit()
    conn.close()
    assert _recorded_provider_key(db) == "onnx"


def test_provider_for_model_names() -> None:
    assert _provider_for_model("BAAI/bge-m3") == "onnx"
    assert _provider_for_model("all-MiniLM-L6-v2") == "local"
    assert _provider_for_model("something-else") is None


def test_missing_index_has_no_recorded_provider(tmp_path: Path) -> None:
    assert _recorded_provider_key(tmp_path / "nope.db") is None

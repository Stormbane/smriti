"""An index follows the root it is given, not the environment's.

``index_tree(root=B)`` and ``index_file_result(path, root=B)`` took their
database from ``SMRITI_ROOT`` whatever root they were given: B's files went
into the environment root's index, and the purge, finding that index's
sources missing under B, deleted them. The database, the embedder (the root's
service, or the provider its index records) and the metrics now follow the
root given.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.indexer import index_file_result, index_tree
from tests._vec import needs_vec

REAL_EMBEDDER = embedding.embedder


def _root(base: Path, name: str, notes: dict[str, str]) -> Path:
    root = base / name
    for rel, text in notes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _sources(root: Path) -> set[str]:
    db = root / ".smriti" / "index.db"
    conn = sqlite3.connect(db)
    try:
        recorded = {r[0] for r in conn.execute("SELECT source FROM sources")}
        chunked = {r[0] for r in conn.execute("SELECT DISTINCT source FROM chunks")}
    finally:
        conn.close()
    assert recorded == chunked
    return recorded


def _events(root: Path, event: str) -> list[dict[str, Any]]:
    path = root / ".smriti" / "metrics.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("event") == event]


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Root A, the environment's, and root B, another; the stub embeds."""
    a = _root(tmp_path, "a", {"identity.md": "# Ari\n\nAri keeps the workshop notes.\n",
                              "notes/kite.md": "# Kite\n\nThe Kite takes AAA cells.\n"})
    b = _root(tmp_path, "b", {"identity.md": "# Sam\n\nSam binds books.\n",
                              "notes/paste.md": "# Paste\n\nWheat starch paste.\n"})
    monkeypatch.setenv("SMRITI_ROOT", str(a))
    monkeypatch.setenv("NARADA_ROOT", str(a))
    stub = StubEmbedding()
    monkeypatch.setattr(embedding, "embedder", lambda: stub)
    return a, b


def test_each_root_given_is_indexed_into_its_own_index(roots: tuple[Path, Path],
                                                       tmp_path: Path) -> None:
    a, b = roots
    elsewhere = _root(tmp_path, "c", {"notes/glue.md": "# Glue\n\nHinge glue dries overnight.\n"})

    index_tree(root=b)
    index_tree(root=elsewhere)

    assert _sources(b) == {"identity.md", "notes/paste.md"}
    assert _sources(elsewhere) == {"notes/glue.md"}
    assert not (a / ".smriti" / "index.db").exists()  # the environment's root: untouched


def test_a_run_on_another_root_neither_writes_into_nor_purges_this_index(
        roots: tuple[Path, Path]) -> None:
    a, b = roots
    index_tree()  # the environment's root
    assert _sources(a) == {"identity.md", "notes/kite.md"}

    stats = index_tree(root=b)

    # Before, B's run used A's index: it added B's files there, and purged
    # A's, whose sources don't exist under B.
    assert stats["purged"] == 0
    assert _sources(a) == {"identity.md", "notes/kite.md"}
    assert _sources(b) == {"identity.md", "notes/paste.md"}
    # Each run's metrics went to its own root.
    assert len(_events(a, "index_completed")) == 1
    assert len(_events(b, "index_completed")) == 1


def test_index_file_on_another_root_leaves_this_index_alone(roots: tuple[Path, Path]) -> None:
    a, b = roots
    index_tree()
    index_tree(root=b)
    new = b / "notes" / "thread.md"
    new.write_text("# Thread\n\nLinen thread, waxed.\n", encoding="utf-8")

    result = index_file_result(new, root=b)

    assert result.chunks == 1
    assert _sources(b) == {"identity.md", "notes/paste.md", "notes/thread.md"}
    assert _sources(a) == {"identity.md", "notes/kite.md"}


@needs_vec  # the result reports stored vectors
def test_index_file_embeds_with_the_service_of_its_root(
        roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the service, a write to root B asks B's service, never A's
    (which would also start A's service and load the model for it)."""
    import smriti.embed.client as client

    a, b = roots
    monkeypatch.setattr(embedding, "embedder", REAL_EMBEDDER)
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    asked: list[Path] = []

    def service_provider(root: Path) -> StubEmbedding:
        asked.append(root)
        return StubEmbedding()

    monkeypatch.setattr(client, "service_provider", service_provider)
    note = b / "notes" / "paste.md"

    result = index_file_result(note, root=b)

    assert result.chunks == 1 and not result.vectors_pending
    assert asked == [b]
    assert _sources(b) == {"notes/paste.md"}
    assert not (a / ".smriti" / "index.db").exists()


def test_smriti_index_with_a_root_indexes_that_root(roots: tuple[Path, Path]) -> None:
    from smriti.cli import main

    a, b = roots

    assert main(["index", "--root", str(b)]) == 0

    assert _sources(b) == {"identity.md", "notes/paste.md"}
    assert not (a / ".smriti" / "index.db").exists()

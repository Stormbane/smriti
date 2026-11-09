"""H15.7: a chunk written twice leaves one row in the keyword index.

The upsert was ``INSERT OR REPLACE INTO chunks``. SQLite fires no delete
trigger for the row a REPLACE removes (unless recursive_triggers is on), so
the keyword index (``chunks_fts``, kept by triggers) kept an entry for a row
that was gone: 2 of 19,677 rows in the reference index. The old row is now
deleted first, which fires the trigger, and its vector goes with it.

One file makes two chunks of one id when two have the same lines and text:
the pieces of a long line that repeats itself, since H15.10 cuts such lines.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.chunker import chunk_markdown
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.schema import IndexDB, ensure_schema, serialize_f32
from tests._vec import needs_vec


def _row(cid: str, content: str, vector: list[float] | None = None) -> indexer._Row:
    blob = serialize_f32(vector) if vector is not None else None
    return (cid, "notes/a.md", "Plans", 2, content, 3, 3, "h", 2, blob)


def _fts_rows(conn: IndexDB) -> int:
    return int(conn.execute("SELECT count(*) FROM chunks_fts_docsize").fetchone()[0])


def _consistent(conn: IndexDB) -> None:
    """FTS5's own check of an external-content index against its table."""
    conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")


def test_the_same_chunk_twice_leaves_one_keyword_row(tmp_path: Path) -> None:
    conn = ensure_schema(tmp_path / "index.db", None)
    try:
        first = indexer._insert_row(conn, _row("c1", "Ari met Sam at the harbour"), "t0")
        second = indexer._insert_row(conn, _row("c1", "Ari met Sam at the lighthouse"), "t1")
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        assert _fts_rows(conn) == 1
        match = "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?"
        assert conn.execute(match, ("lighthouse",)).fetchone()[0] == 1
        assert conn.execute(match, ("harbour",)).fetchone()[0] == 0  # the old text is gone
        _consistent(conn)
        assert (first, second) == (False, True)  # it says when it replaced one
    finally:
        conn.close()


@needs_vec
def test_the_replaced_chunks_vector_goes_with_it(tmp_path: Path) -> None:
    conn = ensure_schema(tmp_path / "index.db", 3)
    try:
        assert conn.has_vec
        indexer._insert_row(conn, _row("c1", "first", [1.0, 0.0, 0.0]), "t0")
        indexer._insert_row(conn, _row("c1", "second", [0.0, 1.0, 0.0]), "t1")
        rows = conn.execute("SELECT c.content FROM chunks c JOIN chunks_vec v "
                            "ON v.rowid = c.rowid").fetchall()
        assert rows == [("second",)]
        assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_file_whose_long_line_repeats_indexes_one_row_per_chunk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tree"
    (root / "notes").mkdir(parents=True)
    text = "# Separator\n\n" + "Ari met Sam. " * 600 + "\n\nAfter the line.\n"
    (root / "notes" / "rule.md").write_text(text, encoding="utf-8")
    chunks = chunk_markdown(text, source="notes/rule.md")
    alike = len(chunks) - len({(c.start_line, c.end_line, c.content) for c in chunks})
    assert alike > 0  # the pieces of the line repeat each other
    monkeypatch.setattr(embedding, "embedder", lambda: StubEmbedding())
    db = tmp_path / "index.db"
    stats = indexer.index_tree(root=root, db=db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        fts = conn.execute("SELECT count(*) FROM chunks_fts_docsize").fetchone()[0]
        conn.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
    finally:
        conn.close()
    assert rows == fts == len(chunks) - alike == stats["chunks"]
    # Writing the file again (index_file, behind smriti_write) keeps it so.
    assert indexer.index_file(root / "notes" / "rule.md", root=root, db=db) == rows
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM chunks_fts_docsize").fetchone()[0] == rows
    finally:
        conn.close()

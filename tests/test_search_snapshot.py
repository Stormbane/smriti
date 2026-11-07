"""A search reads one snapshot of the index, both legs and every chunk's text.

The keyword leg fetched its matches' rowids and ranks, then each chunk's text
in statements of its own, outside any shared read transaction (outside
review #6, Codex 4). A writer replacing chunks in between, SQLite reusing the
freed rowid, paired one chunk's rank with another's text: Codex's
reproduction returned unrelated text at a score of 0.90. These tests run a
writer at that moment, deterministically, and hold that the search returns
exactly what it returns with no writer at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.schema import IndexDB, ensure_schema, open_readonly
from smriti.store.search import search_with_status
from tests._vec import needs_vec

_INSERT = ("INSERT INTO chunks (id, source, heading, heading_level, content, start_line, "
           "end_line, content_hash, trunk_distance, indexed_at) "
           "VALUES (?, ?, '', 0, ?, 1, 1, ?, 1, '2026-05-14')")


class _Rows:
    """A statement's rows, read in full before the writer ran."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._rows)


class _WriterAt(IndexDB):
    """A reader that lets ``writer`` commit once, at the first statement ``when`` picks:
    before it runs, or right after it has returned its rows."""

    def __init__(self, reader: IndexDB, when: Callable[[str], bool], writer: Callable[[], None],
                 *, after: bool) -> None:
        super().__init__(conn=reader.conn, has_vec=reader.has_vec, has_fts=reader.has_fts)
        self._when, self._writer, self._after = when, writer, after
        self.wrote = False

    def execute(self, sql: str, parameters: Sequence[object] | Any = (), /) -> Any:
        if self.wrote or not self._when(sql):
            return super().execute(sql, parameters)
        self.wrote = True
        if not self._after:
            self._writer()
            return super().execute(sql, parameters)
        rows = super().execute(sql, parameters).fetchall()
        self._writer()
        return _Rows(rows)


def _keyword_match(sql: str) -> bool:
    return "chunks_fts MATCH" in sql


def _seed(db: Path) -> None:
    conn = ensure_schema(db, None)
    try:
        if not conn.has_fts:
            pytest.skip("FTS5 not available")
        for i, (source, text) in enumerate([
                ("notes/orchard.md", "The orchard by the well: Sam's apples ripen in October."),
                ("notes/kite.md", "The Kite enclosure uses captive screws."),
                ("notes/cider.md", "Orchard apples, pressed for cider at the end of May.")]):
            conn.execute(_INSERT, (f"c{i}", source, text, f"h{i}"))
        conn.commit()
    finally:
        conn.close()


def _replace_cider_with_unrelated(db: Path) -> Callable[[], None]:
    """The writer: a re-index of two notes that frees cider's rowid (the highest)
    and gives it to an unrelated chunk, as SQLite does."""

    def write() -> None:
        conn = sqlite3.connect(db, timeout=0)  # a WAL reader never blocks it
        try:
            (freed,) = conn.execute(
                "SELECT rowid FROM chunks WHERE source = 'notes/cider.md'").fetchone()
            conn.execute("DELETE FROM chunks WHERE source = 'notes/cider.md'")
            conn.execute(_INSERT, ("c9", "notes/health.md",
                                   "Ari's dentist appointment moved to Thursday.", "h9"))
            (taken,) = conn.execute(
                "SELECT rowid FROM chunks WHERE source = 'notes/health.md'").fetchone()
            assert taken == freed  # the rowid is reused
            conn.commit()
        finally:
            conn.close()

    return write


def _seen(results: Any) -> list[tuple[str, str, float]]:
    return [(r.source, r.content, round(r.score, 6)) for r in results]


def test_keyword_search_never_pairs_a_match_with_another_chunks_text(tmp_path: Path) -> None:
    db = tmp_path / "index.db"
    _seed(db)
    reader = open_readonly(db, vectors=False)
    try:
        before = search_with_status(reader, "orchard apples", use_vectors=False,
                                    use_reranker=False)
        racing = _WriterAt(reader, _keyword_match, _replace_cider_with_unrelated(db),
                           after=True)
        during = search_with_status(racing, "orchard apples", use_vectors=False,
                                    use_reranker=False)
        assert racing.wrote
        after = search_with_status(reader, "orchard apples", use_vectors=False,
                                   use_reranker=False)
    finally:
        reader.close()

    # Before: cider's rank came back with the dentist note's text, scored high.
    assert not any("dentist" in content for _, content, _ in _seen(during.results))
    assert _seen(during.results) == _seen(before.results)
    assert {r.source for r in before.results} == {"notes/orchard.md", "notes/cider.md"}
    # The write is there for the next search, and it doesn't match.
    assert {r.source for r in after.results} == {"notes/orchard.md"}


@needs_vec
def test_a_hybrid_search_reads_both_legs_from_one_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write between the vector leg and the keyword leg: the keyword leg
    scored the new chunk at a rowid the vector leg had read as the old one,
    and the old text came back with the new chunk's keyword score."""
    from smriti.store.indexer import index_tree

    root = tmp_path / "tree"
    (root / "notes").mkdir(parents=True)
    for name, text in [("orchard", "The orchard by the well: Sam's apples ripen in October."),
                       ("kite", "The Kite enclosure uses captive screws."),
                       ("health", "Ari's dentist appointment moved to Thursday.")]:
        (root / "notes" / f"{name}.md").write_text(f"# {name}\n\n{text}\n", encoding="utf-8")
    stub = StubEmbedding()
    monkeypatch.setattr(embedding, "embedder", lambda: stub)
    db = root / ".smriti" / "index.db"
    index_tree(root=root, db=db)

    def rewrite_health_as_a_match() -> None:
        conn = sqlite3.connect(db, timeout=0)
        try:
            conn.execute("UPDATE chunks SET content = ? WHERE source = 'notes/health.md'",
                         ("Orchard apples, orchard apples: the whole orchard of apples.",))
            conn.commit()
        finally:
            conn.close()

    reader = open_readonly(db)
    try:
        assert reader.has_vec
        before = search_with_status(reader, "orchard apples", use_reranker=False)
        racing = _WriterAt(reader, _keyword_match, rewrite_health_as_a_match, after=False)
        during = search_with_status(racing, "orchard apples", use_reranker=False)
        assert racing.wrote
    finally:
        reader.close()

    assert not before.keyword_only and not during.keyword_only
    assert _seen(during.results) == _seen(before.results)
    health = next(r for r in during.results if r.source == "notes/health.md")
    orchard = next(r for r in during.results if r.source == "notes/orchard.md")
    assert health.score < orchard.score

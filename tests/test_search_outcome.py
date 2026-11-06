"""H12.5: ``search_with_status`` builds its results, and logs its metrics, once.

Outside review #5: it built the results and logged the ``search_query``
event in two branches (reranked or not) and repeated ``_fetch_candidate``'s
query inline. One path now serves both; these tests hold what each path
returns and that every search logs exactly one event.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from smriti.store.schema import IndexDB, ensure_schema, open_readonly
from smriti.store.search import search_with_status

DOCS = {
    "notes/lamp.md": "Ari trims the lamp wick before the night watch.",
    "notes/well.md": "Sam draws water from the well, then trims the hedge.",
    "notes/hedge.md": "The hedge by the well needs trimming, says Sam.",
}


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[IndexDB]:
    conn = ensure_schema(tmp_path / "index.db", None)
    if not conn.has_fts:
        conn.close()
        pytest.skip("FTS5 not available")
    for i, (source, text) in enumerate(DOCS.items()):
        conn.execute(
            "INSERT INTO chunks (id, source, heading, heading_level, content, start_line, "
            "end_line, content_hash, trunk_distance, indexed_at) "
            "VALUES (?, ?, '', 0, ?, 1, 1, ?, ?, '2026-11-06')",
            (f"c{i}", source, text, f"h{i}", i))
    conn.commit()
    conn.close()
    reader = open_readonly(tmp_path / "index.db")
    yield reader
    reader.close()


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    logged: list[dict[str, Any]] = []

    class Recorder:
        def log(self, event: str, **fields: Any) -> None:
            logged.append({"event": event, **fields})

    monkeypatch.setattr("smriti.metrics.get_logger", lambda path=None: Recorder())
    return logged


def _search(db: IndexDB, **kw: Any) -> list[tuple[str, float]]:
    out = search_with_status(db, "trims the hedge by the well", use_vectors=False, **kw)
    return [(r.source, round(r.score, 6)) for r in out.results]


def test_without_the_reranker_one_event_and_the_combined_order(
        db: IndexDB, events: list[dict[str, Any]]) -> None:
    got = _search(db, use_reranker=False, top_k=2)
    assert [s for s, _ in got] == ["notes/well.md", "notes/hedge.md"]
    assert len(events) == 1 and events[0]["event"] == "search_query"
    assert events[0]["reranked"] is False and events[0]["final_results"] == 2
    assert events[0]["top_source"] == "notes/well.md" and events[0]["fts_candidates"] == 3


def test_a_reranker_orders_and_scores_them_with_one_event(
        db: IndexDB, events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    def rerank(query: str, items: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        return [dict(item, score=0.5 + i) for i, item in enumerate(reversed(items))][:top_k]

    monkeypatch.setattr("smriti._vendored.memsearch.reranker.rerank", rerank)
    got = _search(db, top_k=2)
    assert got == [("notes/lamp.md", 0.5), ("notes/hedge.md", 1.5)]
    assert len(events) == 1 and events[0]["reranked"] is True
    assert events[0]["top_score"] == 0.5 and events[0]["final_results"] == 2


def test_a_failed_reranker_gives_the_combined_order_with_one_event(
        db: IndexDB, events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    def rerank(*a: object, **k: object) -> list[dict[str, Any]]:
        raise RuntimeError("reranker down")

    monkeypatch.setattr("smriti._vendored.memsearch.reranker.rerank", rerank)
    failed = _search(db, top_k=2)
    assert len(events) == 1 and events[0]["reranked"] is False
    assert failed == _search(db, use_reranker=False, top_k=2)

"""What a memory backup can carry: no raw queries, no index, no private key."""
from __future__ import annotations

from pathlib import Path

import pytest

from smriti.metrics import query_fields

_TEMPLATE_IGNORE = Path(__file__).resolve().parents[1] / "memory_template" / ".gitignore"


def test_queries_are_logged_as_hashes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMRITI_LOG_QUERIES", raising=False)
    fields = query_fields("what did Sam decide about the move")
    assert "query" not in fields
    assert fields["query_len"] == len("what did Sam decide about the move")
    assert len(fields["query_sha"]) == 12
    assert query_fields("same text")["query_sha"] == query_fields("same text")["query_sha"]


def test_raw_queries_are_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMRITI_LOG_QUERIES", "1")
    assert query_fields("debug me")["query"] == "debug me"


def test_search_metrics_carry_no_query_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import smriti.metrics as metrics
    from smriti.store.indexer import index_tree
    from smriti.store.schema import open_readonly
    from smriti.store.search import search

    monkeypatch.delenv("SMRITI_LOG_QUERIES", raising=False)
    root = tmp_path / "tree"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nThe zebrafish tank needs cleaning.\n",
                                   encoding="utf-8")
    db = root / ".smriti" / "index.db"
    index_tree(root=root, db=db)
    log_path = tmp_path / "metrics.jsonl"
    metrics._instance = metrics.MetricsLogger(log_path)
    conn = open_readonly(db)
    try:
        search(conn, "zebrafish tank cleaning schedule", use_vectors=False, use_reranker=False)
    finally:
        conn.close()
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    searches = [e for e in events if e["event"] == "search_query"]
    assert searches and all("query" not in e for e in searches)
    assert "zebrafish" not in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [
    ".smriti/index.db", ".smriti/index.db-wal", ".smriti/metrics.jsonl", ".smriti/recall.jsonl",
    ".smriti/queue.json", "log/.state.json", "log/.writer.lock", "private/.key",
])
def test_memory_template_ignores_runtime_state_and_the_key(path: str) -> None:
    import fnmatch

    patterns = [ln.strip() for ln in _TEMPLATE_IGNORE.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    assert any(fnmatch.fnmatch(path, p.rstrip("/") + ("*" if p.endswith("/") else ""))
               for p in patterns), path


@pytest.mark.parametrize("path", ["identity.md", "journal/2026/11/week1/11-05.md",
                                  "log/2026/11/04-digest.md", "private/README.md"])
def test_memory_template_keeps_the_memory_itself(path: str) -> None:
    import fnmatch

    patterns = [ln.strip() for ln in _TEMPLATE_IGNORE.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    assert not any(fnmatch.fnmatch(path, p) for p in patterns), path

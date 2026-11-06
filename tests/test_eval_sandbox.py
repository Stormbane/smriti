"""Safety regressions for the public, synthetic eval corpus."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from smriti.eval import cases as case_module
from smriti.eval.cases import SEARCH_CASES, SearchCase
from smriti.store.search import SearchResult

from smriti.eval.runner import FIXTURE_ROOT, eval_sandbox, run_search_cases


class LocalEmbedding:
    """Offline test vectors; the real indexer, SQLite and search still run."""

    dimension = 64
    model_name = "test-token-hash"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimension
            for word in re.findall(r"[a-z]+", text.lower()):
                bucket = hashlib.sha256(word.encode()).digest()[0] % self.dimension
                vector[bucket] += 1
            norm = math.sqrt(sum(x * x for x in vector)) or 1
            vectors.append([x / norm for x in vector])
        return vectors


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.store import indexer

    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: LocalEmbedding())


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_live_root_and_cached_metrics_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.metrics as metrics

    live = Path(os.environ["SMRITI_ROOT"])
    shutil.copytree(FIXTURE_ROOT, live, dirs_exist_ok=True)
    # An already-created logger used to bypass environment-only isolation.
    logger = metrics.get_logger()
    logger.log("sentinel", value="preserve me")
    before = snapshot(live)
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert not path.resolve().is_relative_to(live), "eval accessed live memory"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        results = run_search_cases()
    assert len(results) == len(SEARCH_CASES)
    assert snapshot(live) == before
    assert metrics.get_logger() is logger
    assert os.environ["SMRITI_ROOT"] == str(live)


def test_sandbox_is_indexed_and_removed_on_exit() -> None:
    with eval_sandbox() as root:
        assert (root / ".smriti/index.db").is_file()
    assert not root.exists()


def test_fixture_is_synthetic() -> None:
    files = list(FIXTURE_ROOT.rglob("*.md"))
    assert 15 <= len(files) <= 25
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in files)
    corpus += Path(case_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("Narada", "Suti", "bija", "viveka"):
        assert forbidden.lower() not in corpus.lower()
    assert "Ari" in corpus and "Sam" in corpus
    assert len(SEARCH_CASES) >= 12
    assert len({case.id for case in SEARCH_CASES}) == len(SEARCH_CASES)
    for case in SEARCH_CASES:
        for source in case.expected_in_top_k + case.expected_not_in:
            assert (FIXTURE_ROOT / source).is_file()
    for case in SEARCH_CASES:
        if "paraphrase" in case.id:
            target = (FIXTURE_ROOT / case.expected_in_top_k[0]).read_text(encoding="utf-8")
            assert not (
                set(re.findall(r"[a-z]+", case.query.lower()))
                & set(re.findall(r"[a-z]+", target.lower()))
            )


def _can_load_sqlite_extensions() -> bool:
    return hasattr(sqlite3.connect(":memory:"), "enable_load_extension")


@pytest.mark.skipif(not _can_load_sqlite_extensions(),
                    reason="needs vector search: this Python's sqlite3 can't load sqlite-vec "
                           "(the paraphrase cases share no words with their notes, by design)")
def test_search_cases_end_to_end() -> None:
    before = snapshot(FIXTURE_ROOT)
    results = run_search_cases()
    assert len(results) == len(SEARCH_CASES)
    assert results[0].hits == ["identity.md"]
    assert all(result.actual_sources for result in results[:-1])
    assert all(0 <= result.reciprocal_rank <= 1 for result in results)
    assert snapshot(FIXTURE_ROOT) == before


def test_search_scores_abstention_and_newest_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    search = importlib.import_module("smriti.store.search")

    newest = next(case for case in SEARCH_CASES if case.id == "s11-update")
    no_answer = SEARCH_CASES[-1]
    assert newest.k == 1 and newest.expected_not_in

    def response(source: str) -> SearchResult:
        return SearchResult(source, "", "", 1.0, 0, "test")

    with eval_sandbox():
        monkeypatch.setattr(
            search, "search", lambda *a, **kw: [response(newest.expected_not_in[0])]
        )
        results = run_search_cases(cases=[newest, no_answer])
        assert all(not result.passed for result in results)
        monkeypatch.setattr(
            search, "search", lambda *a, **kw: [response(newest.expected_in_top_k[0])]
        )
        assert run_search_cases(cases=[newest])[0].passed
        monkeypatch.setattr(search, "search", lambda *a, **kw: [])
        assert run_search_cases(cases=[no_answer])[0].passed


def test_explicit_search_index_is_readonly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    search = importlib.import_module("smriti.store.search")

    with eval_sandbox() as root:
        db = tmp_path / "custom.db"
        shutil.copy2(root / ".smriti/index.db", db)
    before = snapshot(tmp_path)
    original_search = search.search

    def checked_search(conn: Any, *args: Any, **kwargs: Any) -> Any:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM chunks")
        return original_search(conn, *args, **kwargs)

    monkeypatch.setattr(search, "search", checked_search)
    assert run_search_cases(db_path=db)[0].passed
    assert snapshot(tmp_path) == before

    # A committed WAL must be included, without SQLite touching its source SHM.
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "UPDATE chunks SET content = content || ' snapshotwalneedle' "
            "WHERE source = 'identity.md'"
        )
        writer.commit()
        before = snapshot(tmp_path)
        case = SearchCase("wal", "Committed WAL content", "snapshotwalneedle", ["identity.md"])
        assert run_search_cases(cases=[case], db_path=db)[0].passed
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


def test_index_failure_restores_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.metrics as metrics
    from smriti.store import indexer

    logger = metrics.get_logger()
    previous_env = {name: os.environ.get(name) for name in ("SMRITI_ROOT", "NARADA_ROOT")}
    roots = []

    def fail(*, root: Path, db: Path) -> None:
        roots.append(root)
        assert db == root / ".smriti/index.db"
        raise RuntimeError("synthetic indexing failure")

    monkeypatch.setattr(indexer, "index_tree", fail)
    with pytest.raises(RuntimeError, match="indexing failure"):
        run_search_cases()
    assert roots and not roots[0].exists()
    assert metrics.get_logger() is logger
    assert all(os.environ.get(name) == value for name, value in previous_env.items())


def test_standalone_runner_creates_fresh_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.store import indexer

    roots = []
    original_index = indexer.index_tree

    def track(*, root: Path, db: Path) -> dict[str, int]:
        roots.append(root)
        return original_index(root=root, db=db)

    monkeypatch.setattr(indexer, "index_tree", track)
    run_search_cases()
    run_search_cases()
    assert len(roots) == 2 and roots[0] != roots[1]
    assert all(not root.exists() for root in roots)


def test_cli_baseline_stays_in_sandbox(capsys: Any) -> None:
    from smriti.cli import main

    live = Path(os.environ["SMRITI_ROOT"])
    before = snapshot(live)
    assert main(["eval", "--baseline"]) == 0
    output = capsys.readouterr().out
    saved = next(
        line.removeprefix("Baseline saved: ")
        for line in output.splitlines()
        if line.startswith("Baseline saved: ")
    )
    assert not Path(saved).exists()
    assert not Path(saved).is_relative_to(live)
    assert snapshot(live) == before


def test_cli_explicit_baseline(tmp_path: Path) -> None:
    from smriti.cli import main

    path = tmp_path / "reports" / "baseline.json"
    assert main(["eval", "--baseline", str(path)]) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["search"]["total"] == len(SEARCH_CASES)
    assert set(report) == {"timestamp", "search"}


def test_cli_search_flag_runs_the_default_mode(capsys: Any) -> None:
    """H12.16: `--search` chose the search cases until the other modes went (H3); it
    stopped parsing then. It parses again and runs what `smriti eval` runs."""
    from smriti.cli import main

    def cases(argv: list[str]) -> list[tuple[str, bool]]:
        assert main(argv) == 0
        out = capsys.readouterr().out
        report = json.loads(out[out.index("{"):])
        return [(case["id"], case["passed"]) for case in report["search"]["cases"]]

    plain = cases(["eval", "--json"])
    assert len(plain) == len(SEARCH_CASES)
    assert cases(["eval", "--search", "--json"]) == plain


def test_cli_does_not_initialize_live_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.cli import main
    from smriti.core import tree
    from smriti.private.store import PrivateStore

    live = Path(os.environ["SMRITI_ROOT"])
    (live / "private").mkdir()
    original_root = tree.tree_root

    def guarded_root() -> Path:
        root = original_root()
        assert root != live, "CLI resolved live root before sandbox entry"
        return root

    def forbidden_init(*args: Any, **kwargs: Any) -> None:
        pytest.fail("eval initialized the live vault")

    monkeypatch.setattr(tree, "tree_root", guarded_root)
    monkeypatch.setattr(PrivateStore, "init", forbidden_init)
    assert main(["eval"]) == 0

"""Evaluate fictional memory in disposable, runner-owned trees."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from smriti.eval.cases import SEARCH_CASES, SearchCase

log = logging.getLogger(__name__)
FIXTURE_ROOT = Path(__file__).with_name("fixture")
_active_root: Path | None = None


@contextmanager
def eval_sandbox() -> Iterator[Path]:
    """Copy and index the fixture, then remove it on exit (including on errors).

    Lower-level store code still uses process-wide environment and metrics state.
    Scope both to this copy and restore them afterwards. Eval is a synchronous
    CLI operation; do not run it alongside other store operations in one process.
    It embeds in this process, so it never starts an embedding service for the
    disposable copy.
    """
    import smriti.metrics as metrics
    from smriti.embed.provider import in_process_embeddings
    from smriti.store.indexer import index_tree

    global _active_root
    with TemporaryDirectory(prefix="smriti-eval-") as directory, in_process_embeddings():
        root = Path(directory).resolve()
        shutil.copytree(FIXTURE_ROOT, root, dirs_exist_ok=True)
        previous_root = _active_root
        env_names = ("SMRITI_ROOT", "NARADA_ROOT")
        previous_env = {name: os.environ.get(name) for name in env_names}
        try:
            for name in env_names:
                os.environ[name] = str(root)
            with metrics.logging_to(root / ".smriti" / "metrics.jsonl"):
                _active_root = root
                stats = index_tree(root=root, db=root / ".smriti" / "index.db")
                if stats["errors"] or not stats["chunks"]:
                    raise RuntimeError(f"Could not index eval fixture: {stats}")
                yield root
        finally:
            _active_root = previous_root
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@dataclass
class SearchCaseResult:
    case: SearchCase
    actual_sources: list[str]
    actual_scores: list[float]
    hits: list[str]  # expected sources found in top-k
    misses: list[str]  # expected sources NOT found in top-k
    false_positives: list[str]  # sources in expected_not_in that appeared
    passed: bool
    reciprocal_rank: float


def run_search_cases(
    cases: list[SearchCase] | None = None,
    db_path: Path | None = None,
) -> list[SearchCaseResult]:
    """Search the fixture, or an explicitly supplied index opened read-only.

    Explicit indexes must be quiescent while copied (database plus WAL). Search
    opens that disposable snapshot read-only: even SQLite's WAL bookkeeping
    stays in the sandbox. The source is never opened by SQLite or migrated.
    """
    from smriti.store.schema import open_readonly
    from smriti.store.search import search

    if _active_root is None:
        with eval_sandbox():
            return run_search_cases(cases=cases, db_path=db_path)
    if cases is None:
        cases = SEARCH_CASES
    if db_path is None:
        db_path = _active_root / ".smriti" / "index.db"

    db_path = db_path.resolve()
    if not db_path.is_relative_to(_active_root):
        # mode=ro alone can create -wal/-shm beside the source. Copy first;
        # include committed WAL pages, but let SQLite rebuild shared memory.
        with TemporaryDirectory(prefix="search-", dir=_active_root) as directory:
            copy = Path(directory) / "index.db"
            shutil.copyfile(db_path, copy)
            wal = db_path.with_name(db_path.name + "-wal")
            if wal.exists():
                shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
            return run_search_cases(cases=cases, db_path=copy)

    conn = open_readonly(db_path)
    results = []

    try:
        for case in cases:
            search_results = search(
                conn,
                case.query,
                top_k=case.k,
                use_reranker=False,
            )
            actual_sources = [r.source for r in search_results]
            actual_scores = [r.score for r in search_results]

            # Check hits: which expected sources appeared?
            hits = []
            misses = []
            for expected in case.expected_in_top_k:
                found = any(expected in src for src in actual_sources)
                if found:
                    hits.append(expected)
                else:
                    misses.append(expected)

            # Check false positives
            false_positives = []
            for not_expected in case.expected_not_in:
                found = any(not_expected in src for src in actual_sources)
                if found:
                    false_positives.append(not_expected)

            # Reciprocal rank: rank of the first expected hit
            rr = next(
                (
                    1.0 / (i + 1)
                    for i, src in enumerate(actual_sources)
                    if any(expected in src for expected in case.expected_in_top_k)
                ),
                0.0,
            )

            if not case.expected_in_top_k:
                # An empty gold set means abstention, not an automatic pass.
                false_positives = list(actual_sources)
            passed = len(misses) == 0 and len(false_positives) == 0

            results.append(
                SearchCaseResult(
                    case=case,
                    actual_sources=actual_sources,
                    actual_scores=actual_scores,
                    hits=hits,
                    misses=misses,
                    false_positives=false_positives,
                    passed=passed,
                    reciprocal_rank=rr,
                )
            )

    finally:
        conn.close()
    return results

"""Evaluate fictional memory in disposable, runner-owned trees."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from smriti.eval.cases import (
    CASCADE_CASES,
    JUDGE_CASES,
    SEARCH_CASES,
    CascadeCase,
    JudgeCase,
    SearchCase,
)
from smriti.store.judge import JudgmentResult

log = logging.getLogger(__name__)
FIXTURE_ROOT = Path(__file__).with_name("fixture")
_active_root: Path | None = None
_sandbox_roots: set[Path] = set()


@contextmanager
def eval_sandbox() -> Iterator[Path]:
    """Copy and index the fixture, then remove it on exit (including on errors).

    Lower-level store code still uses process-wide environment and metrics state.
    Scope both to this copy and restore them afterwards. Eval is a synchronous
    CLI operation; do not run it alongside other store operations in one process.
    """
    import smriti.metrics as metrics
    from smriti.store.indexer import index_tree

    global _active_root
    with TemporaryDirectory(prefix="smriti-eval-") as directory:
        root = Path(directory).resolve()
        shutil.copytree(FIXTURE_ROOT, root, dirs_exist_ok=True)
        previous_root = _active_root
        previous_logger = metrics._instance
        env_names = ("SMRITI_ROOT", "NARADA_ROOT")
        previous_env = {name: os.environ.get(name) for name in env_names}
        try:
            for name in env_names:
                os.environ[name] = str(root)
            metrics._instance = metrics.MetricsLogger(root / ".smriti" / "metrics.jsonl")
            _active_root = root
            _sandbox_roots.add(root)
            stats = index_tree(root=root, db=root / ".smriti" / "index.db")
            if stats["errors"] or not stats["chunks"]:
                raise RuntimeError(f"Could not index eval fixture: {stats}")
            yield root
        finally:
            _sandbox_roots.discard(root)
            _active_root = previous_root
            metrics._instance = previous_logger
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@dataclass
class JudgeCaseResult:
    case: JudgeCase
    actual_verdict: str
    actual_direction: str
    passed: bool
    direction_keyword_hits: list[str] = field(default_factory=list)
    direction_keyword_misses: list[str] = field(default_factory=list)


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


@dataclass
class CascadeCaseResult:
    case: CascadeCase
    actual_max_depth: int
    actual_trunk_flag: bool
    passed: bool


# ── JUDGE runner ─────────────────────────────────────────────────────


def run_judge_cases(
    judge_fn: Callable[..., JudgmentResult] | None = None,
    cases: list[JudgeCase] | None = None,
) -> list[JudgeCaseResult]:
    """Run JUDGE eval cases.

    Parameters
    ----------
    judge_fn:
        The judge function to test. Defaults to ``judge_auto_keep``
        (useful for testing the framework itself).
    cases:
        Override the default case set.
    """
    if _active_root is None:
        with eval_sandbox():
            return run_judge_cases(judge_fn=judge_fn, cases=cases)

    if judge_fn is None:
        from smriti.store.judge import judge_auto_keep

        judge_fn = judge_auto_keep

    if cases is None:
        cases = JUDGE_CASES

    results = []
    for case in cases:
        judgment = judge_fn(case.parent_content, case.child_content, None)
        verdict = judgment.verdict

        # Check verdict match
        verdict_match = verdict == case.expected_verdict

        # Check direction keywords (for REVISE cases)
        kw_hits = []
        kw_misses = []
        if case.expected_verdict == "REVISE" and case.expected_direction_keywords:
            direction_lower = judgment.direction.lower()
            for kw in case.expected_direction_keywords:
                if kw.lower() in direction_lower:
                    kw_hits.append(kw)
                else:
                    kw_misses.append(kw)

        passed = verdict_match
        if case.expected_verdict == "REVISE" and case.expected_direction_keywords:
            passed = passed and len(kw_misses) == 0

        results.append(
            JudgeCaseResult(
                case=case,
                actual_verdict=verdict,
                actual_direction=judgment.direction,
                passed=passed,
                direction_keyword_hits=kw_hits,
                direction_keyword_misses=kw_misses,
            )
        )

    return results


# ── Search runner ────────────────────────────────────────────────────


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
            # Search consumes IndexDB; its legacy annotation still says Connection.
            search_results = search(
                conn,  # type: ignore[arg-type]
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


# ── Cascade runner ───────────────────────────────────────────────────


def run_cascade_cases(
    cases: list[CascadeCase] | None = None,
    root: Path | None = None,
) -> list[CascadeCaseResult]:
    """Run mutating cases only in a currently active runner-created copy."""
    from smriti.store.cascade import cognitive_cascade
    from smriti.store.judge import executor_echo, judge_auto_keep

    if root is not None:
        root = root.resolve()
        if root not in _sandbox_roots or root != _active_root:
            raise ValueError("Cascade eval requires an active runner-created sandbox")
    elif _active_root is None:
        with eval_sandbox() as sandbox:
            return run_cascade_cases(cases=cases, root=sandbox)
    else:
        root = _active_root
    if cases is None:
        cases = CASCADE_CASES

    # Reject escaped triggers before making any changes, and reject links in
    # the tree: the cascade scans upstream references throughout the copy.
    for path in root.rglob("*"):
        if not path.resolve().is_relative_to(root):
            raise ValueError("Cascade eval sandbox contains an escaping link")
    for case in cases:
        trigger = (root / case.trigger_file).resolve()
        if not trigger.is_relative_to(root) or trigger == root:
            raise ValueError("Cascade eval trigger escapes the sandbox")

    results = []
    for case in cases:
        trigger = root / case.trigger_file

        # Create parent dirs and write trigger content
        trigger.parent.mkdir(parents=True, exist_ok=True)
        trigger.write_text(case.trigger_content, encoding="utf-8")

        try:
            stats = cognitive_cascade(
                trigger,
                root,
                judge_fn=judge_auto_keep,
                executor_fn=executor_echo,
            )
            actual_depth = stats["max_depth"]
            actual_trunk = len(stats["promoted"]) > 0
        except Exception:
            log.exception("Cascade eval failed: %s", case.id)
            actual_depth = -1
            actual_trunk = False

        passed = (
            actual_depth >= 0
            and actual_depth == case.expected_max_depth
            and actual_trunk == case.expected_trunk_flag
        )

        results.append(
            CascadeCaseResult(
                case=case,
                actual_max_depth=actual_depth,
                actual_trunk_flag=actual_trunk,
                passed=passed,
            )
        )

    return results


# ── Run all ──────────────────────────────────────────────────────────


def run_all(
    judge_fn: Callable[..., JudgmentResult] | None = None,
    skip_cascade: bool = False,
) -> dict[str, Any]:
    """Run all cases in one disposable tree and index."""
    if _active_root is None:
        with eval_sandbox():
            return run_all(judge_fn=judge_fn, skip_cascade=skip_cascade)
    judge_results = run_judge_cases(judge_fn=judge_fn)
    search_results = run_search_cases()
    cascade_results = run_cascade_cases() if not skip_cascade else []

    return {
        "judge": judge_results,
        "search": search_results,
        "cascade": cascade_results,
    }

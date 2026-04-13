"""Run eval cases against the current smriti system."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from smriti.eval.cases import (
    CASCADE_CASES,
    JUDGE_CASES,
    SEARCH_CASES,
    CascadeCase,
    JudgeCase,
    SearchCase,
)


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
    hits: list[str]       # expected sources found in top-k
    misses: list[str]     # expected sources NOT found in top-k
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
    judge_fn=None,
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

        results.append(JudgeCaseResult(
            case=case,
            actual_verdict=verdict,
            actual_direction=judgment.direction,
            passed=passed,
            direction_keyword_hits=kw_hits,
            direction_keyword_misses=kw_misses,
        ))

    return results


# ── Search runner ────────────────────────────────────────────────────


def run_search_cases(
    cases: list[SearchCase] | None = None,
    db_path: Path | None = None,
) -> list[SearchCaseResult]:
    """Run search eval cases against the live index."""
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import ensure_schema
    from smriti.store.search import search

    if cases is None:
        cases = SEARCH_CASES

    if db_path is None:
        db_path = smriti_db_path()

    if not db_path.exists():
        raise RuntimeError("No index. Run 'smriti index' first.")

    tmp = sqlite3.connect(str(db_path))
    dim_row = tmp.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    tmp.close()
    dim = int(dim_row[0]) if dim_row else 384

    conn = ensure_schema(db_path, dim)
    results = []

    for case in cases:
        search_results = search(conn, case.query, top_k=case.k, use_reranker=False)
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
        rr = 0.0
        for expected in case.expected_in_top_k:
            for i, src in enumerate(actual_sources):
                if expected in src:
                    rr = 1.0 / (i + 1)
                    break
            if rr > 0:
                break

        passed = len(misses) == 0 and len(false_positives) == 0

        results.append(SearchCaseResult(
            case=case,
            actual_sources=actual_sources,
            actual_scores=actual_scores,
            hits=hits,
            misses=misses,
            false_positives=false_positives,
            passed=passed,
            reciprocal_rank=rr,
        ))

    conn.close()
    return results


# ── Cascade runner ───────────────────────────────────────────────────


def run_cascade_cases(
    cases: list[CascadeCase] | None = None,
    root: Path | None = None,
) -> list[CascadeCaseResult]:
    """Run cascade eval cases.

    Note: cascade cases need a real tree to test against. For unit testing,
    use the test fixtures in test_cascade.py. This runner is for integration
    testing against the live tree.
    """
    from smriti.core.tree import tree_root
    from smriti.store.cascade import cognitive_cascade
    from smriti.store.judge import executor_echo, judge_auto_keep

    if cases is None:
        cases = CASCADE_CASES
    if root is None:
        root = tree_root()

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
            actual_depth = -1
            actual_trunk = False

        passed = True
        if case.expected_trunk_flag and not actual_trunk:
            passed = False

        results.append(CascadeCaseResult(
            case=case,
            actual_max_depth=actual_depth,
            actual_trunk_flag=actual_trunk,
            passed=passed,
        ))

    return results


# ── Run all ──────────────────────────────────────────────────────────


def run_all(
    judge_fn=None,
    skip_cascade: bool = False,
) -> dict:
    """Run all eval cases. Returns structured results."""
    judge_results = run_judge_cases(judge_fn=judge_fn)
    search_results = run_search_cases()
    cascade_results = run_cascade_cases() if not skip_cascade else []

    return {
        "judge": judge_results,
        "search": search_results,
        "cascade": cascade_results,
    }

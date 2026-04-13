"""Format eval results for terminal or JSON output."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from smriti.eval.metrics import EvalMetrics
from smriti.eval.runner import CascadeCaseResult, JudgeCaseResult, SearchCaseResult


def terminal_report(
    metrics: EvalMetrics,
    judge_results: list[JudgeCaseResult],
    search_results: list[SearchCaseResult],
    cascade_results: list[CascadeCaseResult],
    *,
    verbose: bool = False,
) -> str:
    """Format a human-readable terminal report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"smriti eval — {now}",
        "=" * 40,
        "",
    ]

    # ── JUDGE ────────────────────────────────────────────────────────
    if judge_results:
        lines.append(f"JUDGE ({metrics.judge_total} cases)")
        lines.append(f"  Accuracy:    {metrics.judge_passed}/{metrics.judge_total} ({metrics.judge_accuracy:.0%})")

        # Per-verdict breakdown
        for expected, actuals in sorted(metrics.judge_by_verdict.items()):
            correct = actuals.get(expected, 0)
            total = sum(actuals.values())
            lines.append(f"  {expected:8s}:    {correct}/{total}")

        if verbose:
            lines.append("")
            for r in judge_results:
                status = "PASS" if r.passed else "FAIL"
                lines.append(f"  [{status}] {r.case.id}: expected={r.case.expected_verdict}, got={r.actual_verdict}")
                if not r.passed:
                    lines.append(f"         {r.case.description}")
                    if r.direction_keyword_misses:
                        lines.append(f"         missing keywords: {r.direction_keyword_misses}")
        lines.append("")

    # ── Search ───────────────────────────────────────────────────────
    if search_results:
        lines.append(f"SEARCH ({metrics.search_total} cases)")
        lines.append(f"  MRR:         {metrics.search_mrr:.2f}")
        lines.append(f"  Recall@k:    {metrics.search_recall_at_k:.0%}")
        lines.append(f"  Passed:      {metrics.search_passed}/{metrics.search_total}")

        if verbose:
            lines.append("")
            for r in search_results:
                status = "PASS" if r.passed else "FAIL"
                lines.append(f"  [{status}] {r.case.id}: {r.case.query}")
                if r.misses:
                    lines.append(f"         missing: {r.misses}")
                if r.false_positives:
                    lines.append(f"         false positives: {r.false_positives}")
        lines.append("")

    # ── Cascade ──────────────────────────────────────────────────────
    if cascade_results:
        lines.append(f"CASCADE ({metrics.cascade_total} cases)")
        lines.append(f"  Avg depth:   {metrics.cascade_avg_depth:.1f}")
        lines.append(f"  Trunk flags: {metrics.cascade_trunk_flag_rate:.0%}")
        lines.append(f"  Passed:      {metrics.cascade_passed}/{metrics.cascade_total}")
        lines.append("")

    # ── Cost ──────────────────────────────────────────────────────────
    if metrics.total_tokens_in > 0 or metrics.total_cost_usd > 0:
        lines.append("COST (from metrics log)")
        lines.append(f"  Tokens in:   {metrics.total_tokens_in:,}")
        lines.append(f"  Tokens out:  {metrics.total_tokens_out:,}")
        lines.append(f"  Est. cost:   ${metrics.total_cost_usd:.4f}")
        if metrics.avg_judge_latency_ms > 0:
            lines.append(f"  Avg judge:   {metrics.avg_judge_latency_ms:.0f}ms")
        if metrics.avg_search_latency_ms > 0:
            lines.append(f"  Avg search:  {metrics.avg_search_latency_ms:.0f}ms")
        lines.append("")

    return "\n".join(lines)


def json_report(
    metrics: EvalMetrics,
    judge_results: list[JudgeCaseResult],
    search_results: list[SearchCaseResult],
    cascade_results: list[CascadeCaseResult],
) -> str:
    """Format a JSON report for the dashboard."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "judge": {
            "total": metrics.judge_total,
            "passed": metrics.judge_passed,
            "accuracy": round(metrics.judge_accuracy, 4),
            "by_verdict": metrics.judge_by_verdict,
            "cases": [
                {
                    "id": r.case.id,
                    "expected": r.case.expected_verdict,
                    "actual": r.actual_verdict,
                    "passed": r.passed,
                }
                for r in judge_results
            ],
        },
        "search": {
            "total": metrics.search_total,
            "passed": metrics.search_passed,
            "mrr": round(metrics.search_mrr, 4),
            "recall_at_k": round(metrics.search_recall_at_k, 4),
            "cases": [
                {
                    "id": r.case.id,
                    "query": r.case.query,
                    "hits": r.hits,
                    "misses": r.misses,
                    "passed": r.passed,
                    "rr": round(r.reciprocal_rank, 4),
                }
                for r in search_results
            ],
        },
        "cascade": {
            "total": metrics.cascade_total,
            "passed": metrics.cascade_passed,
            "avg_depth": round(metrics.cascade_avg_depth, 2),
            "trunk_flag_rate": round(metrics.cascade_trunk_flag_rate, 4),
        },
        "cost": {
            "tokens_in": metrics.total_tokens_in,
            "tokens_out": metrics.total_tokens_out,
            "cost_usd": round(metrics.total_cost_usd, 4),
            "avg_judge_ms": round(metrics.avg_judge_latency_ms, 1),
            "avg_search_ms": round(metrics.avg_search_latency_ms, 1),
        },
    }
    return json.dumps(data, indent=2)

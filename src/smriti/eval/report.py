"""Format eval results for terminal or JSON output."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from smriti.eval.metrics import EvalMetrics
from smriti.eval.runner import SearchCaseResult


def terminal_report(
    metrics: EvalMetrics,
    search_results: list[SearchCaseResult],
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

    if search_results:
        lines.append(f"SEARCH ({metrics.search_total} cases)")
        lines.append(f"  MRR:         {metrics.search_mrr:.2f}")
        lines.append(f"  Recall@k:    {metrics.search_recall_at_k:.0%}")
        lines.append(f"  Passed:      {metrics.search_passed}/{metrics.search_total}")
        if metrics.avg_search_latency_ms > 0:
            lines.append(f"  Avg search:  {metrics.avg_search_latency_ms:.0f}ms")

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

    return "\n".join(lines)


def json_report(metrics: EvalMetrics, search_results: list[SearchCaseResult]) -> str:
    """Format a JSON report, for saving as a baseline."""
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "search": {
            "total": metrics.search_total,
            "passed": metrics.search_passed,
            "mrr": round(metrics.search_mrr, 4),
            "recall_at_k": round(metrics.search_recall_at_k, 4),
            "avg_ms": round(metrics.avg_search_latency_ms, 1),
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
    }
    return json.dumps(data, indent=2)

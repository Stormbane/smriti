"""Compute evaluation metrics from runner results and metrics.jsonl."""

from __future__ import annotations

from dataclasses import dataclass

from smriti.eval.runner import SearchCaseResult


@dataclass
class EvalMetrics:
    """Aggregate metrics from an eval run."""

    search_total: int = 0
    search_passed: int = 0
    search_mrr: float = 0.0
    search_recall_at_k: float = 0.0

    # From metrics.jsonl, populated separately.
    avg_search_latency_ms: float = 0.0


def compute_metrics(search_results: list[SearchCaseResult]) -> EvalMetrics:
    """Compute aggregate metrics from raw results."""
    m = EvalMetrics()

    if search_results:
        m.search_total = len(search_results)
        m.search_passed = sum(1 for r in search_results if r.passed)

        # MRR: mean reciprocal rank
        rrs = [r.reciprocal_rank for r in search_results if r.case.expected_in_top_k]
        m.search_mrr = sum(rrs) / len(rrs) if rrs else 0

        # Recall@k: fraction of expected results found
        total_expected = sum(len(r.case.expected_in_top_k) for r in search_results)
        total_hits = sum(len(r.hits) for r in search_results)
        m.search_recall_at_k = total_hits / total_expected if total_expected else 0

    return m


def enrich_from_metrics_log(
    eval_metrics: EvalMetrics,
    since: str | None = None,
) -> EvalMetrics:
    """Add search latency from metrics.jsonl to eval metrics."""
    from smriti.metrics import get_logger

    summary = get_logger().summary(since=since)
    eval_metrics.avg_search_latency_ms = summary.get("avg_search_ms", 0.0)
    return eval_metrics

"""Compute evaluation metrics from runner results and metrics.jsonl."""

from __future__ import annotations

from dataclasses import dataclass, field

from smriti.eval.runner import CascadeCaseResult, JudgeCaseResult, SearchCaseResult


@dataclass
class EvalMetrics:
    """Aggregate metrics from an eval run."""

    # JUDGE
    judge_total: int = 0
    judge_passed: int = 0
    judge_accuracy: float = 0.0
    judge_by_verdict: dict[str, dict[str, int]] = field(default_factory=dict)

    # Search
    search_total: int = 0
    search_passed: int = 0
    search_mrr: float = 0.0
    search_recall_at_k: float = 0.0

    # Cascade
    cascade_total: int = 0
    cascade_passed: int = 0
    cascade_avg_depth: float = 0.0
    cascade_trunk_flag_rate: float = 0.0

    # Cost (from metrics.jsonl, populated separately)
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cost_usd: float = 0.0
    avg_judge_latency_ms: float = 0.0
    avg_search_latency_ms: float = 0.0


def compute_metrics(
    judge_results: list[JudgeCaseResult],
    search_results: list[SearchCaseResult],
    cascade_results: list[CascadeCaseResult],
) -> EvalMetrics:
    """Compute aggregate metrics from raw results."""
    m = EvalMetrics()

    # ── JUDGE metrics ────────────────────────────────────────────────
    if judge_results:
        m.judge_total = len(judge_results)
        m.judge_passed = sum(1 for r in judge_results if r.passed)
        m.judge_accuracy = m.judge_passed / m.judge_total if m.judge_total else 0

        # Confusion matrix: expected → actual → count
        confusion: dict[str, dict[str, int]] = {}
        for r in judge_results:
            expected = r.case.expected_verdict
            actual = r.actual_verdict
            if expected not in confusion:
                confusion[expected] = {}
            confusion[expected][actual] = confusion[expected].get(actual, 0) + 1
        m.judge_by_verdict = confusion

    # ── Search metrics ───────────────────────────────────────────────
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

    # ── Cascade metrics ──────────────────────────────────────────────
    if cascade_results:
        m.cascade_total = len(cascade_results)
        m.cascade_passed = sum(1 for r in cascade_results if r.passed)
        depths = [r.actual_max_depth for r in cascade_results if r.actual_max_depth >= 0]
        m.cascade_avg_depth = sum(depths) / len(depths) if depths else 0
        trunk_flags = sum(1 for r in cascade_results if r.actual_trunk_flag)
        m.cascade_trunk_flag_rate = trunk_flags / m.cascade_total if m.cascade_total else 0

    return m


def enrich_from_metrics_log(
    eval_metrics: EvalMetrics,
    since: str | None = None,
) -> EvalMetrics:
    """Add cost/latency data from metrics.jsonl to eval metrics."""
    from smriti.metrics import get_logger

    logger = get_logger()
    summary = logger.summary(since=since)

    eval_metrics.total_tokens_in = summary.get("total_tokens_in", 0)
    eval_metrics.total_tokens_out = summary.get("total_tokens_out", 0)
    eval_metrics.total_cost_usd = summary.get("total_cost_usd", 0.0)
    eval_metrics.avg_search_latency_ms = summary.get("avg_search_ms", 0.0)

    # Judge latency from cascade_verdict events
    verdicts = logger.read(event_type="cascade_verdict", since=since)
    if verdicts:
        latencies = [v.get("judge_ms", 0) for v in verdicts if v.get("judge_ms", 0) > 0]
        eval_metrics.avg_judge_latency_ms = sum(latencies) / len(latencies) if latencies else 0

    return eval_metrics

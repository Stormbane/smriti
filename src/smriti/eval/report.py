"""Format eval results for the terminal, as JSON, or as Markdown (CI's job summary)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from smriti.eval.digests import DigestReport
from smriti.eval.metrics import EvalMetrics
from smriti.eval.quality import KS, THRESHOLD, QualityReport
from smriti.eval.runner import SearchCaseResult


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _quality_lines(quality: QualityReport) -> list[str]:
    d = quality.as_dict()
    lines = [f"QUALITY SET v{d['version']} ({d['cases']} cases, embedder: "
             f"{d['embedder'] or 'none'})"]
    if d["keyword_only"]:
        lines.append(f"  keyword-only: {d['keyword_only']}")
    lines.append("  retrieval      n   " + "  ".join(f"hit@{k}" for k in KS) + "   MRR")
    for kind, row in d["retrieval"].items():
        hits = "  ".join(f"{_pct(row[f'hit@{k}']):>5}" for k in KS)
        lines.append(f"  {kind:<13} {int(row['n']):>2}   {hits}  {row['mrr']:.2f}")
    for kind, row in d["disagreement"].items():
        lines.append(f"  {kind}: resolved@5 {_pct(row['resolved@5'])}, "
                     f"newer first {_pct(row['newer_first'])}")
    score = d["abstention"].get("search_score")
    if score:
        lines.append(f"  abstention at score {THRESHOLD}: irrelevant abstained "
                     f"{_pct(score['abstained'])}, answerable abstained "
                     f"{_pct(score['false_abstention'])}; AUROC {score['auroc']:.2f}")
    sim = d["abstention"].get("similarity")
    if sim:
        lines.append(f"  abstention on raw similarity: AUROC {sim['auroc']:.2f}")
    return lines


def _digest_lines(digests: DigestReport) -> list[str]:
    lines = ["DIGESTS (a real LLM: coverage of turns, facts stated, sentences traceable)"]
    for name, score in digests.days.items():
        lines.append(f"  {name}: coverage {_pct(score.coverage)}, facts {_pct(score.facts)}, "
                     f"traceable {_pct(score.traceable)}")
        for fact in score.missed_facts:
            lines.append(f"    missed: {fact}")
        for sentence in score.untraceable:
            lines.append(f"    untraceable: {sentence[:100]}")
    means = digests.means()
    if means:
        lines.append(f"  mean: coverage {_pct(means['coverage'])}, facts {_pct(means['facts'])}, "
                     f"traceable {_pct(means['traceable'])}")
    for name, error in digests.errors.items():
        lines.append(f"  {name}: FAILED: {error}")
    return lines


def terminal_report(
    metrics: EvalMetrics,
    search_results: list[SearchCaseResult],
    *,
    verbose: bool = False,
    quality: QualityReport | None = None,
    digests: DigestReport | None = None,
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

    if quality is not None:
        lines.extend(_quality_lines(quality))
        if verbose:
            for q in quality.results:
                lines.append(f"    {q.case.id}: first relevant {q.first_relevant or '-'}, "
                             f"top {q.sources[:3]}")
        lines.append("")
    if digests is not None:
        lines.extend(_digest_lines(digests))
        lines.append("")

    return "\n".join(lines)


def json_report(metrics: EvalMetrics, search_results: list[SearchCaseResult], *,
                quality: QualityReport | None = None,
                digests: DigestReport | None = None) -> str:
    """Format a JSON report, for saving as a baseline."""
    data: dict[str, Any] = {
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
    if quality is not None:
        data["quality"] = _rounded(quality.as_dict())
    if digests is not None:
        data["digests"] = digests.as_dict()
    return json.dumps(data, indent=2)


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def markdown_report(metrics: EvalMetrics, quality: QualityReport | None,
                    digests: DigestReport | None = None) -> str:
    """The scores as Markdown tables, for a CI job summary; the digests' too with ``--llm``."""
    lines = ["## smriti eval", "",
             f"Search cases: {metrics.search_passed}/{metrics.search_total} passed, "
             f"MRR {metrics.search_mrr:.2f}.", ""]
    if quality is not None:
        lines += _quality_markdown(quality) + [""]
    if digests is not None:
        lines += _digest_markdown(digests) + [""]
    return "\n".join(lines)


def _cell(text: str) -> str:
    """*text* safe inside one Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _quality_markdown(quality: QualityReport) -> list[str]:
    d = quality.as_dict()
    lines = [f"Quality set v{d['version']}, {d['cases']} cases; embedder "
             f"`{d['embedder'] or 'none'}`"
             + (f"; keyword-only: {d['keyword_only']}" if d["keyword_only"] else "") + ".",
             "", "| kind | n | " + " | ".join(f"hit@{k}" for k in KS) + " | MRR |",
             "|---|---|" + "---|" * len(KS) + "---|"]
    for kind, row in d["retrieval"].items():
        hits = " | ".join(_pct(row[f"hit@{k}"]) for k in KS)
        lines.append(f"| {kind} | {int(row['n'])} | {hits} | {row['mrr']:.2f} |")
    lines += ["", "| disagreement | n | resolved@5 | newer first |", "|---|---|---|---|"]
    for kind, row in d["disagreement"].items():
        lines.append(f"| {kind} | {int(row['n'])} | {_pct(row['resolved@5'])} | "
                     f"{_pct(row['newer_first'])} |")
    lines += ["", "| abstention | irrelevant abstained | answerable abstained | AUROC |",
              "|---|---|---|---|"]
    for name, row in d["abstention"].items():
        label = f"search score < {THRESHOLD}" if name == "search_score" else "raw similarity"
        auroc = "-" if row.get("auroc") is None else f"{row['auroc']:.2f}"
        lines.append(f"| {label} | {_pct(row.get('abstained'))} | "
                     f"{_pct(row.get('false_abstention'))} | {auroc} |")
    return lines


def _digest_markdown(digests: DigestReport) -> list[str]:
    """Each synthetic day's scores, or why it has none, then what each one missed."""
    lines = ["Digests, made with a real LLM (`--llm`):", "",
             "| digest | coverage | facts | traceable |", "|---|---|---|---|"]
    for name, score in digests.days.items():
        lines.append(f"| {name} | {_pct(score.coverage)} | {_pct(score.facts)} | "
                     f"{_pct(score.traceable)} |")
    for name, error in digests.errors.items():
        lines.append(f"| {name} | FAILED: {_cell(error)} | | |")
    means = digests.means()
    if means:
        lines.append(f"| mean | {_pct(means['coverage'])} | {_pct(means['facts'])} | "
                     f"{_pct(means['traceable'])} |")
    notes = [f"- {name}: missed: {fact}" for name, score in digests.days.items()
             for fact in score.missed_facts]
    notes += [f"- {name}: untraceable: {sentence[:100]}"
              for name, score in digests.days.items() for sentence in score.untraceable]
    return lines + ([""] + notes if notes else [])

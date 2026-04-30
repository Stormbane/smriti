"""Backend dispatch + JSONL logging for recall fires."""

from __future__ import annotations

import json
import time
from pathlib import Path

from smriti.recall.config import RecallConfig, load_config
from smriti.recall.types import RecallResponse


def _log(log_path: Path, entry: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def run_recall(
    query: str,
    *,
    cfg: RecallConfig | None = None,
    log_extra: dict | None = None,
) -> RecallResponse:
    """Run a recall query against the configured backend, logging the fire.

    ``log_extra`` is merged into the JSONL log entry — typically the
    triggering tool name and file path from the PostToolUse payload.
    """
    cfg = cfg or load_config()

    if cfg.backend == "smriti":
        from smriti.recall.backends import smriti_be as backend
    else:
        from smriti.recall.backends import qmd as backend

    response = backend.query(
        query, top_k=cfg.top_k, timeout_s=cfg.timeout_s, rerank=cfg.rerank,
    )

    # Threshold answers "is this relevant?" (backend's job). Rerank
    # answers "in what order?" (trunk's job). They are independent: filter
    # first on the raw backend score so trunk re-blending can never
    # demote a relevant match below the cutoff.
    relevant = [m for m in response.matches if m.score >= cfg.threshold]

    # Trunk-distance rerank: blend backend relevance with how
    # identity-adjacent each surviving result is. No-op at alpha=0.
    if cfg.trunk_alpha > 0 and relevant:
        from smriti.recall.rerank import rerank as _trunk_rerank
        relevant = _trunk_rerank(
            relevant,
            alpha=cfg.trunk_alpha,
            collection=cfg.collection,
        )

    injected_chars = 0
    for m in relevant[:cfg.max_inject]:
        injected_chars += len(m.source) + len(m.snippet[:200]) + 20

    entry: dict = {
        "ts": time.time(),
        "query": query,
        "elapsed_ms": response.elapsed_ms,
        "backend": response.backend,
        "match_count_total": len(response.matches),
        "match_count_relevant": len(relevant),
        "top_score": response.matches[0].score if response.matches else 0.0,
        "top_source": response.matches[0].source if response.matches else "",
        "injected": min(len(relevant), cfg.max_inject),
        "injected_chars": injected_chars,
        "injected_tokens_est": injected_chars // 4,
    }
    if response.error:
        entry["error"] = response.error
    if log_extra:
        entry.update(log_extra)

    _log(cfg.log_path, entry)

    response.matches = relevant[:cfg.max_inject]
    return response

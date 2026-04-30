"""Recall configuration — env-driven, no config file needed.

Environment variables:
    SMRITI_RECALL_BACKEND     ``qmd`` (default) or ``smriti``.
    SMRITI_RECALL_THRESHOLD   Min score to inject. Default 0.45.
    SMRITI_RECALL_TOP_K       Candidates to fetch. Default 4.
    SMRITI_RECALL_MAX_INJECT  Max matches to inject per fire. Default 2.
    SMRITI_RECALL_TIMEOUT_S   Backend timeout. Default 20.
    SMRITI_RECALL_RERANK      Set to ``1`` to opt into qmd's reranker.
                              Default off due to upstream qmd issue #519
                              (CUDA crash on Windows + RTX). Re-enable
                              once that lands.
    SMRITI_RECALL_LOG_PATH    Override JSONL log path.
                              Default ``~/.narada/.smriti/recall.jsonl``.
    SMRITI_RECALL_QMD_URL     Base URL for qmd's HTTP daemon.
                              Default ``http://localhost:8181``.
    SMRITI_RECALL_NO_HTTP     Set to ``1`` to disable the HTTP fast
                              path even if the daemon is reachable.
                              Useful for debugging the subprocess
                              fallback. Default off.
    SMRITI_RECALL_TRUNK_ALPHA Weight of the trunk-distance boost in the
                              final score. ``final = (1-alpha) *
                              qmd_score + alpha * trunk_boost``.
                              Default 0.2 — trunk breaks ties without
                              dethroning strong matches. Set to 0 to
                              disable reranking entirely.
    SMRITI_RECALL_COLLECTION  qmd collection name to search. Default
                              ``narada``. Used both as the
                              ``collections`` filter on /query and as
                              the prefix to strip when computing trunk
                              distance.
    SMRITI_RECALL_INTENT      Free-text steering signal passed to qmd
                              as ``intent``. Disambiguates without
                              changing the query (e.g. "ambient
                              context for editing source code").
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_LOG = Path.home() / ".narada" / ".smriti" / "recall.jsonl"


@dataclass(frozen=True)
class RecallConfig:
    backend: str
    threshold: float
    top_k: int
    max_inject: int
    timeout_s: float
    rerank: bool
    log_path: Path
    qmd_url: str
    no_http: bool
    trunk_alpha: float
    collection: str
    intent: str | None


def load_config() -> RecallConfig:
    backend = os.environ.get("SMRITI_RECALL_BACKEND", "qmd").strip().lower()
    if backend not in ("qmd", "smriti"):
        backend = "qmd"
    intent_raw = os.environ.get("SMRITI_RECALL_INTENT", "").strip()
    return RecallConfig(
        backend=backend,
        threshold=float(os.environ.get("SMRITI_RECALL_THRESHOLD", "0.45")),
        top_k=int(os.environ.get("SMRITI_RECALL_TOP_K", "4")),
        max_inject=int(os.environ.get("SMRITI_RECALL_MAX_INJECT", "2")),
        timeout_s=float(os.environ.get("SMRITI_RECALL_TIMEOUT_S", "20")),
        rerank=os.environ.get("SMRITI_RECALL_RERANK", "").strip() == "1",
        log_path=Path(os.environ.get("SMRITI_RECALL_LOG_PATH", str(_DEFAULT_LOG))),
        qmd_url=os.environ.get(
            "SMRITI_RECALL_QMD_URL", "http://localhost:8181",
        ).rstrip("/"),
        no_http=os.environ.get("SMRITI_RECALL_NO_HTTP", "").strip() == "1",
        trunk_alpha=float(os.environ.get("SMRITI_RECALL_TRUNK_ALPHA", "0.2")),
        collection=os.environ.get("SMRITI_RECALL_COLLECTION", "narada"),
        intent=intent_raw or None,
    )

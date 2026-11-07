"""Recall configuration — env-driven, no config file needed.

Environment variables:
    SMRITI_RECALL_THRESHOLD   Min score to inject. Default 0.45.
    SMRITI_RECALL_TOP_K       Candidates to fetch. Default 4.
    SMRITI_RECALL_MAX_INJECT  Max matches to inject per fire. Default 2.
    SMRITI_RECALL_TIMEOUT_S   Backend timeout. Default 20.
    SMRITI_RECALL_RERANK      Set to ``1`` to rerank with the
                              cross-encoder when vectors are on
                              (``SMRITI_RECALL_VECTORS=1``). Default off.
    SMRITI_RECALL_LOG_PATH    Override JSONL log path.
                              Default ``<root>/.smriti/recall.jsonl``.
    SMRITI_RECALL_TRUNK_ALPHA Weight of the trunk-distance boost in the
                              final score. ``final = (1-alpha) *
                              score + alpha * trunk_boost``.
                              Default 0.2 — trunk breaks ties without
                              dethroning strong matches. Set to 0 to
                              disable reranking entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from smriti.core.tree import tree_root


@dataclass(frozen=True)
class RecallConfig:
    threshold: float
    top_k: int
    max_inject: int
    timeout_s: float
    rerank: bool
    log_path: Path
    trunk_alpha: float


def load_config() -> RecallConfig:
    return RecallConfig(
        threshold=float(os.environ.get("SMRITI_RECALL_THRESHOLD", "0.45")),
        top_k=int(os.environ.get("SMRITI_RECALL_TOP_K", "4")),
        max_inject=int(os.environ.get("SMRITI_RECALL_MAX_INJECT", "2")),
        timeout_s=float(os.environ.get("SMRITI_RECALL_TIMEOUT_S", "20")),
        rerank=os.environ.get("SMRITI_RECALL_RERANK", "").strip() == "1",
        log_path=Path(os.environ.get("SMRITI_RECALL_LOG_PATH")
                      or tree_root() / ".smriti" / "recall.jsonl"),
        trunk_alpha=float(os.environ.get("SMRITI_RECALL_TRUNK_ALPHA", "0.2")),
    )

"""Associative recall — pluggable backends for ambient memory injection.

The PostToolUse hook on Read/Edit/Write tools queries the configured
backend (qmd by default, smriti's embedded search as a fallback) and
injects high-relevance matches into the assistant's context as a
``<system-reminder>`` block.

Public API:
    run_recall(query, top_k) -> list[RecallMatch]
    RecallMatch — backend-agnostic match shape
    load_config() -> RecallConfig
"""

from __future__ import annotations

from smriti.recall.config import RecallConfig, load_config
from smriti.recall.types import RecallMatch
from smriti.recall.runner import run_recall

__all__ = ["RecallConfig", "RecallMatch", "load_config", "run_recall"]

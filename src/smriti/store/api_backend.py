"""Backwards-compatible LLM call shim.

This module preserves the original public surface (``call_api``,
``CallMetadata``, ``DEFAULT_MODEL``, ``DEFAULT_EXECUTOR_MODEL``,
``MAX_TOKENS``) used by the consolidate, journal_rollup, judge,
threads, summarize, actionables, and watcher modules. Under the hood it
now delegates to ``smriti.llm.call_llm`` so the actual provider is
pluggable (Anthropic SDK, claude -p, OpenAI, Ollama, ...).

To switch backends, set ``SMRITI_LLM_PROVIDER`` (anthropic_api,
claude_cli, openai_api, ollama). Default auto-detect prefers the
Anthropic SDK when ``ANTHROPIC_API_KEY`` is set.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from smriti.llm import call_llm
from smriti.llm.types import LLMError, RateLimitExceeded  # noqa: F401  (re-export)

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("SMRITI_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_EXECUTOR_MODEL = os.environ.get("SMRITI_EXECUTOR_MODEL", "claude-sonnet-4-6-20250514")
MAX_TOKENS = 4096


@dataclass
class CallMetadata:
    """Legacy metadata shape — kept for callers that destructure it."""

    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


def call_api(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = MAX_TOKENS,
) -> tuple[str, CallMetadata]:
    """Call the configured LLM provider with prompt caching when supported.

    Preserves the original (text, CallMetadata) return shape.
    """
    response = call_llm(
        system=system, user=user,
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
    )
    meta = CallMetadata(
        model=response.model,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        cache_read_tokens=response.cache_read_tokens,
        cache_creation_tokens=response.cache_creation_tokens,
        cost_usd=response.cost_usd,
        elapsed_ms=response.elapsed_ms,
    )
    if meta.cache_read_tokens > 0:
        log.info(
            "API call: model=%s in=%d (cached=%d) out=%d cost=$%.4f %dms",
            meta.model, meta.tokens_in, meta.cache_read_tokens,
            meta.tokens_out, meta.cost_usd, meta.elapsed_ms,
        )
    else:
        log.info(
            "API call: model=%s in=%d out=%d cost=$%.4f %dms",
            meta.model, meta.tokens_in, meta.tokens_out,
            meta.cost_usd, meta.elapsed_ms,
        )
    return response.text, meta

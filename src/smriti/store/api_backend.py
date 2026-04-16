"""Anthropic API backend with prompt caching for smriti LLM calls.

Replaces ``claude -p`` subprocess calls with direct Anthropic SDK usage.
System prompts are marked with ``cache_control`` so repeated calls within
the 5-minute TTL get a 90% discount on those input tokens.

Usage::

    from smriti.store.api_backend import call_api

    text, meta = call_api(
        system="You are a JUDGE...",
        user="--- PARENT ---\n...\n--- CHILD ---\n...",
        model="claude-haiku-4-5-20251001",
    )

Falls back to ``claude -p`` if the ``anthropic`` SDK is not installed or
``ANTHROPIC_API_KEY`` is not set.

Model selection via ``SMRITI_MODEL`` env var (default: claude-haiku-4-5-20251001):
- JUDGE calls: fast, cheap, discrimination-not-generation → Haiku
- EXECUTOR calls: need capability → caller can override with model= arg
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("SMRITI_MODEL", "claude-haiku-4-5-20251001")
DEFAULT_EXECUTOR_MODEL = os.environ.get("SMRITI_EXECUTOR_MODEL", "claude-sonnet-4-6-20250514")
MAX_TOKENS = 4096

_client = None


@dataclass
class CallMetadata:
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic
    except ImportError:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    _client = anthropic.Anthropic(api_key=key)
    return _client


def _estimate_cost(model: str, meta: CallMetadata) -> float:
    """Rough cost estimate from token counts."""
    rates = {
        "claude-haiku-4-5-20251001": (0.80, 4.0, 0.08),
        "claude-sonnet-4-6-20250514": (3.0, 15.0, 0.30),
        "claude-opus-4-6-20250514": (15.0, 75.0, 1.50),
    }
    in_rate, out_rate, cache_read_rate = rates.get(
        model, rates["claude-sonnet-4-6-20250514"]
    )
    fresh_in = meta.tokens_in - meta.cache_read_tokens
    cost = (
        (fresh_in / 1_000_000) * in_rate
        + (meta.cache_read_tokens / 1_000_000) * cache_read_rate
        + (meta.tokens_out / 1_000_000) * out_rate
    )
    return round(cost, 6)


def call_api(
    *,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = MAX_TOKENS,
) -> tuple[str, CallMetadata]:
    """Call the Anthropic API with prompt caching on the system prompt.

    Falls back to ``claude -p`` if the SDK is unavailable.
    """
    client = _get_client()
    model = model or DEFAULT_MODEL

    if client is None:
        return _fallback_claude_p(system, user)

    t0 = time.monotonic()
    meta = CallMetadata(model=model)

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user}],
    )

    meta.elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta.tokens_in = response.usage.input_tokens
    meta.tokens_out = response.usage.output_tokens
    meta.cache_read_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    meta.cache_creation_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    meta.cost_usd = _estimate_cost(model, meta)

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    if meta.cache_read_tokens > 0:
        log.info(
            "API call: model=%s in=%d (cached=%d) out=%d cost=$%.4f %dms",
            model, meta.tokens_in, meta.cache_read_tokens,
            meta.tokens_out, meta.cost_usd, meta.elapsed_ms,
        )
    else:
        log.info(
            "API call: model=%s in=%d out=%d cost=$%.4f %dms",
            model, meta.tokens_in, meta.tokens_out, meta.cost_usd, meta.elapsed_ms,
        )

    return text, meta


def _fallback_claude_p(system: str, user: str) -> tuple[str, CallMetadata]:
    """Fall back to claude -p when the SDK is not available."""
    from smriti.store.judge import _call_claude, CallMetadata as OldMeta

    prompt = f"{system}\n\n{user}"
    text, old_meta = _call_claude(prompt)
    meta = CallMetadata(
        model=old_meta.model,
        tokens_in=old_meta.tokens_in,
        tokens_out=old_meta.tokens_out,
        cost_usd=old_meta.cost_usd,
        elapsed_ms=old_meta.elapsed_ms,
    )
    return text, meta

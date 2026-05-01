"""Pluggable JUDGE and EXECUTOR functions for the cognitive cascade.

The JUDGE decides whether a parent abstraction needs updating.
The EXECUTOR generates revised content per the JUDGE's direction.

For v0.1: both roles are played by ``claude -p`` (two separate calls).
For testing: ``judge_auto_keep`` and ``executor_echo`` skip LLM calls.
Eventually: Qwen3+LoRA as JUDGE, Claude as EXECUTOR.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# RateLimitExceeded now lives in smriti.llm.types and is re-exported
# here so existing ``from smriti.store.judge import RateLimitExceeded``
# imports keep working.
from smriti.llm.types import RateLimitExceeded  # noqa: F401

log = logging.getLogger(__name__)


@dataclass
class CallMetadata:
    """Metadata from a claude -p call, for metrics logging."""

    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


@dataclass
class JudgmentResult:
    """The JUDGE's output."""

    seeing: str
    verdict: str  # KEEP | REVISE | REJECT | PROMOTE
    direction: str  # what to change (REVISE only)
    reason: str
    meta: CallMetadata = field(default_factory=CallMetadata)


# ── Test implementations (no LLM) ───────────────────────────────────


def judge_auto_keep(
    parent_content: str,
    child_content: str,
    prompt_path: Path | None = None,
) -> JudgmentResult:
    """Always returns KEEP. For testing cascade plumbing without LLM calls."""
    return JudgmentResult(
        seeing="Auto-keep: no LLM evaluation performed.",
        verdict="KEEP",
        direction="",
        reason="Testing mode — auto-keep.",
    )


def executor_echo(
    parent_content: str,
    direction: str,
    child_content: str,
    prompt_path: Path | None = None,
) -> str:
    """Returns parent unchanged. For testing without LLM calls."""
    return parent_content


# ── Legacy claude -p shim ───────────────────────────────────────────
#
# Kept for callers that import ``_call_claude`` / ``_get_claude_path``
# directly (consolidate, router, api_backend's old fallback). The real
# implementation now lives in ``smriti.llm.providers.claude_cli`` and is
# selected through the provider factory.


def _get_claude_path() -> str:
    from smriti.llm.providers.claude_cli import ClaudeCliProvider
    return ClaudeCliProvider()._path()


_DEFAULT_CLAUDE_TIMEOUT = int(os.environ.get("NARADA_CLAUDE_TIMEOUT", "300"))


def _call_claude(prompt: str, *, timeout: int | None = None) -> tuple[str, CallMetadata]:
    """Legacy entry point — delegates to the LLM provider factory.

    Kept so direct importers (consolidate, router) keep working. New
    code should use ``smriti.llm.call_llm`` directly.
    """
    from smriti.llm.providers.claude_cli import ClaudeCliProvider
    from smriti.llm.types import LLMRequest

    provider = ClaudeCliProvider()
    request = LLMRequest(
        system="", user=prompt,
        max_tokens=4096,
        timeout_s=timeout if timeout is not None else _DEFAULT_CLAUDE_TIMEOUT,
    )
    response = provider.call(request)
    meta = CallMetadata(
        model=response.model,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        cost_usd=response.cost_usd,
        elapsed_ms=response.elapsed_ms,
    )
    return response.text, meta


def judge_via_claude(
    parent_content: str,
    child_content: str,
    prompt_path: Path | None = None,
) -> JudgmentResult:
    """Call the JUDGE via Anthropic API (with prompt caching) or claude -p fallback."""
    if prompt_path and prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
    else:
        template = (
            "You are reviewing whether a parent abstraction needs updating "
            "given a new child. Respond as JSON with keys: seeing, verdict "
            "(KEEP/REVISE/REJECT/PROMOTE), direction, reason."
        )

    from smriti.store.api_backend import call_api, DEFAULT_MODEL
    system = f"{template}\n\nRespond as JSON only."
    user = (
        f"--- PARENT ---\n{parent_content}\n\n"
        f"--- CHILD (new or changed) ---\n{child_content}"
    )
    raw, api_meta = call_api(system=system, user=user, model=DEFAULT_MODEL)
    meta = CallMetadata(
        model=api_meta.model, tokens_in=api_meta.tokens_in,
        tokens_out=api_meta.tokens_out, cost_usd=api_meta.cost_usd,
        elapsed_ms=api_meta.elapsed_ms,
    )

    # Parse JSON from response
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            return JudgmentResult(
                seeing=data.get("seeing", ""),
                verdict=data.get("verdict", "KEEP"),
                direction=data.get("direction", ""),
                reason=data.get("reason", ""),
                meta=meta,
            )
    except json.JSONDecodeError:
        pass

    log.warning("Could not parse JUDGE response as JSON, defaulting to KEEP")
    return JudgmentResult(
        seeing=raw[:500],
        verdict="KEEP",
        direction="",
        reason="Could not parse structured response.",
        meta=meta,
    )


def executor_via_claude(
    parent_content: str,
    direction: str,
    child_content: str,
    prompt_path: Path | None = None,
) -> str:
    """Call the EXECUTOR via Anthropic API (with prompt caching) or claude -p fallback.

    Returns the revised content as a string.
    """
    if prompt_path and prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
    else:
        template = (
            "Revise the page following the direction. Return ONLY the "
            "revised page content as markdown."
        )

    from smriti.store.api_backend import call_api, DEFAULT_EXECUTOR_MODEL
    system = f"{template}\n\nReturn ONLY the revised page content."
    user = (
        f"--- CURRENT PAGE ---\n{parent_content}\n\n"
        f"--- DIRECTION FROM VIVEKA ---\n{direction}\n\n"
        f"--- CONTEXT ---\n{child_content}"
    )
    text, meta = call_api(system=system, user=user, model=DEFAULT_EXECUTOR_MODEL)
    log.info(
        "EXECUTOR: model=%s tokens_in=%d (cached=%d) tokens_out=%d cost=$%.4f elapsed=%dms",
        meta.model, meta.tokens_in, meta.cache_read_tokens,
        meta.tokens_out, meta.cost_usd, meta.elapsed_ms,
    )
    return text


def summarize_via_claude(prompt: str) -> tuple[str, CallMetadata]:
    """Run a single-prompt summarization via the API backend.

    Used by journal rollups and wake-context rebuild — operations that
    are pure summarization rather than parent+direction+child revision.
    Returns (summary_text, metadata) so callers can log metrics.
    """
    from smriti.store.api_backend import call_api, DEFAULT_EXECUTOR_MODEL
    system = (
        "You produce clear, faithful summaries of the supplied material. "
        "Return ONLY the summary content. No preamble, no meta-commentary."
    )
    text, api_meta = call_api(system=system, user=prompt, model=DEFAULT_EXECUTOR_MODEL)
    meta = CallMetadata(
        model=getattr(api_meta, "model", ""),
        tokens_in=getattr(api_meta, "tokens_in", 0),
        tokens_out=getattr(api_meta, "tokens_out", 0),
        cost_usd=getattr(api_meta, "cost_usd", 0.0),
        elapsed_ms=getattr(api_meta, "elapsed_ms", 0),
    )
    log.info(
        "SUMMARIZE: model=%s tokens_in=%d tokens_out=%d cost=$%.4f",
        meta.model, meta.tokens_in, meta.tokens_out, meta.cost_usd,
    )
    return text, meta

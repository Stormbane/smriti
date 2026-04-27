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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class RateLimitExceeded(RuntimeError):
    """Raised when claude -p reports the subscription tier limit is hit.

    Callers (sleep dispatcher) catch this and stop the cluster/cascade
    loop cleanly so we don't burn through every queued task with the
    same failure until the reset window.
    """


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


# ── Claude -p implementations ───────────────────────────────────────


# Resolve the claude CLI absolute path once, so we bypass per-spawn PATH
# lookup. Observed 2026-04-15 on Windows: the 3rd consecutive subprocess
# call (summary + route + revise) would fail with FileNotFoundError
# despite `claude` being on PATH. Using the absolute path avoids that.
_CLAUDE_PATH: str | None = None


def _get_claude_path() -> str:
    global _CLAUDE_PATH
    if _CLAUDE_PATH is None:
        import shutil
        resolved = shutil.which("claude")
        _CLAUDE_PATH = resolved if resolved else "claude"
        if resolved:
            log.debug("Resolved claude CLI to %s", resolved)
    return _CLAUDE_PATH


_DEFAULT_CLAUDE_TIMEOUT = int(os.environ.get("NARADA_CLAUDE_TIMEOUT", "300"))


def _call_claude(prompt: str, *, timeout: int | None = None) -> tuple[str, CallMetadata]:
    """Call ``claude -p`` and return ``(text, metadata)``.

    Parses the JSON response for token counts, cost, and model info.

    Uses the absolute path to ``claude`` resolved once via shutil.which to
    avoid per-spawn PATH lookup flakiness on Windows. Retries once on
    FileNotFoundError as a last-resort safety net.

    Long prompts are piped via stdin because Windows CreateProcess rejects
    command lines above ~32KB with a misleading "filename too long" error.
    A summarization prompt assembling a week's journal entries easily
    exceeds that.
    """
    if timeout is None:
        timeout = _DEFAULT_CLAUDE_TIMEOUT
    t0 = time.monotonic()
    meta = CallMetadata()
    claude = _get_claude_path()

    # Windows command-line limit is ~32KB. Use stdin for anything near that.
    # Safe threshold: 8KB leaves plenty of headroom for the rest of the
    # argv.
    use_stdin = len(prompt) > 8000
    if use_stdin:
        cmd = [claude, "-p", "--output-format", "json"]
        stdin_text: str | None = prompt
    else:
        cmd = [claude, "-p", prompt, "--output-format", "json"]
        stdin_text = None

    # SMRITI_INTERNAL=1 lets SessionEnd hooks (e.g. backup.py) skip on
    # smriti-internal subprocesses; otherwise they fire per claude -p call
    # and pile up, getting cancelled, which fails the LLM call.
    env = {**os.environ, "SMRITI_INTERNAL": "1"}

    def _spawn() -> subprocess.CompletedProcess:
        # Force UTF-8 for I/O. Windows defaults to cp1252 which fails on
        # unicode arrows, em-dashes, devanagari, etc. that regularly
        # appear in journal content.
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            input=stdin_text, env=env,
            encoding="utf-8", errors="replace",
        )

    try:
        try:
            result = _spawn()
        except FileNotFoundError:
            log.warning("claude CLI not found on first try; retrying once")
            time.sleep(0.5)
            try:
                result = _spawn()
            except FileNotFoundError:
                raise RuntimeError(
                    f"claude CLI not found at '{claude}'. Is Claude Code installed?"
                )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p timed out after {timeout}s")

    meta.elapsed_ms = int((time.monotonic() - t0) * 1000)

    if result.returncode != 0:
        stderr = (result.stderr or "")[:500]
        stdout = (result.stdout or "")[:500]
        # Detect Claude Code subscription rate-limit so callers can stop
        # the dispatcher cleanly instead of failing every subsequent task.
        # Smriti runs claude -p exclusively when ANTHROPIC_API_KEY is unset;
        # once the subscription tier limit is hit, all further calls fail
        # the same way until the reset window.
        #
        # Empirically observed signature: exit code 1 with empty stderr.
        # The rate-limit message (when present) goes to stdout, but often
        # claude -p exits before emitting any output. Treat exit=1 with
        # blank stderr AND blank stdout as suspected rate-limit too.
        rate_markers = (
            "rate limit", "rate_limit", "5-hour limit",
            "usage limit", "quota", "limit reached",
        )
        combined = (stderr + " " + stdout).lower()
        if any(m in combined for m in rate_markers):
            raise RateLimitExceeded(stderr or stdout or "(no output)")
        if result.returncode == 1 and not stderr.strip() and not stdout.strip():
            raise RateLimitExceeded("exit 1 with no output (suspected limit)")
        raise RuntimeError(f"claude -p exit {result.returncode}: {stderr}")

    if not result.stdout.strip():
        raise RuntimeError("claude -p returned empty output")

    # Parse JSON response
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip(), meta

    # Extract metadata from claude -p JSON response
    meta.model = data.get("model", "")
    meta.tokens_in = data.get("input_tokens", 0)
    meta.tokens_out = data.get("output_tokens", 0)
    meta.cost_usd = data.get("total_cost_usd", 0.0)

    # Extract the text content
    text = data.get("result", "")
    if not text:
        text = data.get("content", data.get("text", result.stdout.strip()))
    return text, meta


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

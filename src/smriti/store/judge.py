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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

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


def _call_claude(prompt: str, *, timeout: int = 120) -> tuple[str, CallMetadata]:
    """Call ``claude -p`` and return ``(text, metadata)``.

    Parses the JSON response for token counts, cost, and model info.

    Uses the absolute path to ``claude`` resolved once via shutil.which to
    avoid per-spawn PATH lookup flakiness on Windows. Retries once on
    FileNotFoundError as a last-resort safety net.
    """
    t0 = time.monotonic()
    meta = CallMetadata()
    claude = _get_claude_path()
    cmd = [claude, "-p", prompt, "--output-format", "json"]

    def _spawn() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
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
        raise RuntimeError(f"claude -p exit {result.returncode}: {result.stderr[:500]}")

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
    """Call ``claude -p`` with the JUDGE prompt."""
    if prompt_path and prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
    else:
        template = (
            "You are reviewing whether a parent abstraction needs updating "
            "given a new child. Respond as JSON with keys: seeing, verdict "
            "(KEEP/REVISE/REJECT/PROMOTE), direction, reason."
        )

    prompt = (
        f"{template}\n\n"
        f"--- PARENT ---\n{parent_content}\n\n"
        f"--- CHILD (new or changed) ---\n{child_content}\n\n"
        f"Respond as JSON only."
    )

    raw, meta = _call_claude(prompt)

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
    """Call ``claude -p`` with the EXECUTOR prompt.

    Returns the revised content as a string. Metadata is logged internally.
    """
    if prompt_path and prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
    else:
        template = (
            "Revise the page following the direction. Return ONLY the "
            "revised page content as markdown."
        )

    prompt = (
        f"{template}\n\n"
        f"--- CURRENT PAGE ---\n{parent_content}\n\n"
        f"--- DIRECTION FROM VIVEKA ---\n{direction}\n\n"
        f"--- CONTEXT ---\n{child_content}\n\n"
        f"Return ONLY the revised page content."
    )

    text, meta = _call_claude(prompt)
    log.info(
        "EXECUTOR: model=%s tokens_in=%d tokens_out=%d cost=$%.4f elapsed=%dms",
        meta.model, meta.tokens_in, meta.tokens_out, meta.cost_usd, meta.elapsed_ms,
    )
    return text

"""Codex CLI provider — shells out to ``codex exec``.

The nightly cycle's fallback seat (spec: subscription seats only, no
per-token billing). ``codex exec`` runs one non-interactive turn and
prints the final message to stdout.

Isolation (diff review P1s): ``--ephemeral`` so internal calls persist
no rollout for the day-log daemon to ingest; ``--sandbox read-only`` +
``-c mcp_servers={}`` so transcript-derived prompt content cannot
trigger writes, exec, or the user's MCP servers; ``--cd`` into the
empty smriti LLM workdir so even reads see nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)

_RATE_MARKERS = ("rate limit", "usage limit", "quota", "limit reached", "too many requests")


class CodexCliProvider:
    name = "codex_cli"

    def __init__(self) -> None:
        self._cli_path: str | None = None

    def _path(self) -> str:
        if self._cli_path is None:
            self._cli_path = shutil.which("codex") or "codex"
        return self._cli_path

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def default_model(self, role: str = "executor") -> str:
        return ""  # whatever the ChatGPT subscription wires up

    def call(self, request: LLMRequest) -> LLMResponse:
        turns = request.turns()
        body = "\n\n".join(
            m.content if m.role == "user" else f"Assistant: {m.content}" for m in turns
        )
        prompt = f"{request.system}\n\n{body}" if request.system else body
        timeout = request.timeout_s or int(os.environ.get("SMRITI_CODEX_TIMEOUT", "300"))

        from smriti.llm.workdir import llm_workdir

        cmd = [
            self._path(), "exec", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", "read-only", "-c", "mcp_servers={}",
            "--cd", str(llm_workdir()),
        ]
        if request.model:
            cmd.extend(["--model", request.model])
        if request.cli_args:
            cmd.extend(request.cli_args)
        cmd.append("-")  # read the prompt from stdin (argv limits on Windows)

        t0 = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                input=prompt, encoding="utf-8", errors="replace",
                env={**os.environ, "SMRITI_INTERNAL": "1"},
            )
        except FileNotFoundError as exc:
            raise LLMError("codex CLI not found. Is Codex installed?") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"codex exec timed out after {timeout}s") from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if result.returncode != 0:
            combined = ((result.stderr or "") + " " + (result.stdout or "")).lower()
            if any(m in combined for m in _RATE_MARKERS):
                raise RateLimitExceeded((result.stderr or result.stdout)[:300])
            snippet = (result.stderr or result.stdout or "(no output)")[:300]
            raise LLMError(f"codex exec exit {result.returncode}: {snippet}")

        text = (result.stdout or "").strip()
        if not text:
            raise LLMError("codex exec returned empty output")
        return LLMResponse(text=text, elapsed_ms=elapsed_ms)

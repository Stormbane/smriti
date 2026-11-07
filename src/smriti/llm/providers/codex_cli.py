"""Codex CLI provider — shells out to ``codex exec``.

The nightly cycle's fallback seat when one is configured (spec:
subscription seats only, no per-token billing; opt-in since hardening
H12.2). ``codex exec`` runs one non-interactive turn and prints the final
message to stdout.

Isolation, and what it doesn't cover:

- ``--ephemeral``: the call persists no rollout for capture to ingest.
- ``-c mcp_servers={}``: none of the user's MCP servers load.
- ``--sandbox read-only``: the commands codex runs can't write files or
  reach the network. They still run, and they can read any file the user
  can: an absolute path (``~/.ssh``, another project, the memory tree)
  reads as well as it does without the sandbox, and how strictly the
  sandbox holds depends on the platform. A prompt injection in the
  transcripts the call reads can make it copy what it reads into its
  answer, which becomes a digest that is indexed, committed and pushed.
- ``--cd`` into the empty smriti LLM workdir: it starts with no project's
  files or AGENTS.md around it. That is only where it starts; it limits
  no read. The user's own Codex configuration still loads, apart from
  its MCP servers.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

if sys.platform == "win32":
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _CREATE_NO_WINDOW = 0

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
                creationflags=_CREATE_NO_WINDOW,
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

"""Claude Code CLI provider — shells out to ``claude -p``.

Default for users with a Claude Code subscription and no API key. The
absolute path to the CLI is resolved once via shutil.which to avoid
per-spawn PATH lookup flakiness on Windows. Long prompts are piped via
stdin because Windows CreateProcess rejects argv > 32KB.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

if sys.platform == "win32":
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _CREATE_NO_WINDOW = 0

log = logging.getLogger(__name__)


_RATE_MARKERS = (
    "rate limit", "rate_limit", "5-hour limit", "usage limit",
    "quota", "limit reached", "limit reset",
)


class ClaudeCliProvider:
    name = "claude_cli"

    def __init__(self) -> None:
        self._cli_path: str | None = None

    def _path(self) -> str:
        if self._cli_path is None:
            resolved = shutil.which("claude")
            self._cli_path = resolved or "claude"
            if resolved:
                log.debug("Resolved claude CLI to %s", resolved)
        return self._cli_path

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def default_model(self, role: str = "executor") -> str:
        # The CLI uses whatever model the active subscription wires up;
        # we don't pass --model unless the caller explicitly set one.
        return ""

    def call(self, request: LLMRequest) -> LLMResponse:
        # ``claude -p`` is single-shot. For multi-turn we render the
        # message list with role markers; the model is generally good
        # at recognizing the convention. Future: route through
        # ``--resume <session-id>`` to preserve context server-side.
        turns = request.turns()
        if len(turns) == 1 and turns[0].role == "user":
            user_block = turns[0].content
        else:
            lines = []
            for m in turns:
                tag = "Human" if m.role == "user" else "Assistant"
                lines.append(f"{tag}: {m.content}")
            lines.append("Assistant:")
            user_block = "\n\n".join(lines)
        prompt = user_block
        timeout = request.timeout_s or int(
            os.environ.get("NARADA_CLAUDE_TIMEOUT", "300")
        )
        t0 = time.monotonic()
        cli = self._path()

        # ALWAYS pipe the prompt via stdin. On Windows ``claude`` resolves
        # to npm's ``claude.cmd`` shim, and cmd.exe ends a command at the
        # first newline: a prompt in argv is cut to its first line, and
        # every flag after it is lost with it (the tool limits included).
        cmd = [cli, "-p", "--output-format", "json"]
        stdin_text: str | None = prompt
        if request.model:
            cmd.extend(["--model", request.model])
        if request.cli_args:
            cmd.extend(request.cli_args)

        # SMRITI_INTERNAL=1 lets SessionEnd hooks (backup.py) skip on
        # smriti-internal subprocesses so they don't pile up and fail
        # the LLM call.
        env = {**os.environ, "SMRITI_INTERNAL": "1"}

        # Run from the dedicated LLM workdir: claude -p writes a session
        # transcript under ~/.claude/projects/<encoded-cwd>/, and the
        # day-log collector excludes the llm-workdir namespace so
        # internal calls never pollute the day-log.
        # Fail CLOSED if the workdir can't be created: running from the
        # ambient cwd would recreate the writeback path, so surface an
        # LLMError and let the caller's fallback chain take over.
        from smriti.llm.workdir import llm_workdir

        try:
            workdir = str(llm_workdir())
        except OSError as exc:
            raise LLMError(f"cannot create isolated LLM workdir: {exc}") from exc

        # Isolation (renewal plan 0.4). The nightly audit found every internal
        # call carrying the user's global CLAUDE.md and its hooks (a "SESSION
        # ENDING ... MUST smriti_write" prompt hook, a Stop hook), which made
        # digests open with preambles instead of content. --setting-sources
        # drops user settings and their hooks; the workdir's own settings file
        # (llm_workdir) excludes every CLAUDE.md; and smriti's instructions
        # become the actual system prompt instead of riding in the user turn
        # under Claude Code's agentic one. The file form is needed because
        # cmd.exe would cut a multi-line argv value at its first newline.
        cmd.extend(["--setting-sources", "project,local"])
        if "--strict-mcp-config" not in cmd:  # user-scope MCP servers load regardless of settings
            cmd.append("--strict-mcp-config")
        sys_file: Path | None = None
        if request.system:
            sys_file = Path(workdir) / f".system-{uuid.uuid4().hex}.txt"
            try:
                sys_file.write_text(request.system, encoding="utf-8")
            except OSError as exc:
                raise LLMError(f"cannot write the system prompt file: {exc}") from exc
            cmd.extend(["--system-prompt-file", str(sys_file)])

        def _spawn() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                input=stdin_text, env=env, cwd=workdir,
                encoding="utf-8", errors="replace",
                # hidden console: from a windowless caller (pythonw task,
                # daemon) claude and every shell it runs would otherwise flash
                creationflags=_CREATE_NO_WINDOW,
            )

        try:
            try:
                result = _spawn()
            except FileNotFoundError:
                log.warning("claude CLI not found on first try; retrying once")
                time.sleep(0.5)
                try:
                    result = _spawn()
                except FileNotFoundError as exc:
                    raise LLMError(
                        f"claude CLI not found at {cli!r}. Is Claude Code installed?"
                    ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p timed out after {timeout}s") from exc
        finally:
            if sys_file is not None:
                sys_file.unlink(missing_ok=True)

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if result.returncode != 0:
            self._raise_for_failure(result)

        if not result.stdout.strip():
            raise LLMError("claude -p returned empty output")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return LLMResponse(text=result.stdout.strip(), elapsed_ms=elapsed_ms)

        text = data.get("result") or data.get("content") or data.get("text") or ""
        if not text:
            text = result.stdout.strip()

        return LLMResponse(
            text=text,
            model=data.get("model", ""),
            tokens_in=data.get("input_tokens", 0),
            tokens_out=data.get("output_tokens", 0),
            cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            elapsed_ms=elapsed_ms,
        )

    def _raise_for_failure(self, result: subprocess.CompletedProcess[str]) -> None:
        stderr = (result.stderr or "")[:500]
        stdout_full = result.stdout or ""
        stdout = stdout_full[:500]

        # claude -p emits errors as JSON when --output-format=json is set;
        # rate-limits show ``is_error: true`` plus a human message.
        rl_signal = ""
        try:
            data = json.loads(stdout_full) if stdout_full.strip() else {}
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            blob = " ".join(
                str(v) for v in (
                    data.get("result"), data.get("error"),
                    data.get("message"), data.get("subtype"),
                ) if v
            ).lower()
            if data.get("is_error") and blob and any(m in blob for m in _RATE_MARKERS):
                rl_signal = blob[:300]
        if not rl_signal:
            combined = (stderr + " " + stdout).lower()
            if any(m in combined for m in _RATE_MARKERS):
                rl_signal = (stderr or stdout)[:300]
        if rl_signal:
            raise RateLimitExceeded(rl_signal)
        # Empirical signature: exit 1 with no output is silent rate-limit.
        if result.returncode == 1 and not stderr.strip() and not stdout.strip():
            raise RateLimitExceeded("exit 1 with no output (suspected limit)")
        snippet = stderr or stdout[:200] or "(no output)"
        raise LLMError(f"claude -p exit {result.returncode}: {snippet}")

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
import time

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

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
        prompt = (
            f"{request.system}\n\n{user_block}"
            if request.system else user_block
        )
        timeout = request.timeout_s or int(
            os.environ.get("NARADA_CLAUDE_TIMEOUT", "300")
        )
        t0 = time.monotonic()
        cli = self._path()

        # Argv limit on Windows is ~32KB. Pipe via stdin past 8KB.
        use_stdin = len(prompt) > 8000
        if use_stdin:
            cmd = [cli, "-p", "--output-format", "json"]
            stdin_text: str | None = prompt
        else:
            cmd = [cli, "-p", prompt, "--output-format", "json"]
            stdin_text = None
        if request.model:
            cmd.extend(["--model", request.model])

        # SMRITI_INTERNAL=1 lets SessionEnd hooks (backup.py) skip on
        # smriti-internal subprocesses so they don't pile up and fail
        # the LLM call.
        env = {**os.environ, "SMRITI_INTERNAL": "1"}

        def _spawn() -> subprocess.CompletedProcess:
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
                    raise LLMError(
                        f"claude CLI not found at {cli!r}. Is Claude Code installed?"
                    )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude -p timed out after {timeout}s") from exc

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

    def _raise_for_failure(self, result: subprocess.CompletedProcess) -> None:
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

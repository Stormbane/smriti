"""Per-source adapters: transcript bytes in, conversational turns out.

Each adapter consumes the *new* bytes of one source file (from the
persisted offset) and returns an :class:`ExtractResult`:

- ``turns`` — the conversational turns found;
- ``consumed`` — how many of the given bytes were consumed. Only
  complete, newline-terminated lines are consumed; a partial tail is
  left for the next pass;
- ``errors`` — count of malformed/unknown records skipped. Never raises
  on bad input: unknown schemas are counted and skipped, not fatal;
- ``carry`` — what the adapter needs back with the next read of the same
  file, kept in that file's capture state and saved with its offset; None
  leaves what was carried unchanged. It lets an adapter decide the same
  way however the file's bytes are split between reads, and across
  restarts. It is JSON (lists of strings and numbers), small, and holds
  no message text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from smriti.daylog.model import Turn


@dataclass
class ExtractResult:
    turns: list[Turn] = field(default_factory=list)
    consumed: int = 0
    errors: int = 0
    carry: list[list[Any]] | None = None


class Adapter(Protocol):
    def extract(self, path: Path, blob: bytes,
                carry: list[list[Any]] | None = None) -> ExtractResult: ...


def complete_lines(blob: bytes) -> tuple[list[bytes], int]:
    """Split *blob* into newline-terminated lines; return (lines, consumed)."""
    end = blob.rfind(b"\n")
    if end < 0:
        return [], 0
    consumed = end + 1
    return blob[:consumed].split(b"\n")[:-1], consumed


# Transcript namespaces produced by smriti's OWN internal LLM subprocesses
# (see smriti.llm.workdir): never captured, or derived text would feed back
# into future digests. Claude Code names the transcript's directory after
# the session's cwd; a Codex rollout's path doesn't say, only its cwd does.
INTERNAL_MARKERS = ("llm-workdir",)


def is_internal(path_or_cwd: str) -> bool:
    """Whether a transcript path, or a session's working directory, is smriti's own."""
    return any(marker in path_or_cwd for marker in INTERNAL_MARKERS)


def project_of(cwd: str) -> str:
    """The last component of a session's working directory, from any platform.

    Transcripts carry the cwd of the machine that wrote them, so a Windows
    path must name its project on Linux too (``Path`` there would keep the
    whole ``C:\\...`` string as one name).
    """
    return re.split(r"[\\/]", cwd.rstrip("\\/"))[-1]


def get_adapter(kind: str) -> Adapter:
    if kind == "claude_jsonl":
        from smriti.daylog.adapters.claude_jsonl import ClaudeJsonlAdapter

        return ClaudeJsonlAdapter()
    if kind == "codex_rollout":
        from smriti.daylog.adapters.codex_rollout import CodexRolloutAdapter

        return CodexRolloutAdapter()
    if kind == "voice_md":
        from smriti.daylog.adapters.voice_md import VoiceMdAdapter

        return VoiceMdAdapter()
    raise ValueError(f"unknown adapter kind {kind!r}")

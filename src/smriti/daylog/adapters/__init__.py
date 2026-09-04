"""Per-source adapters: transcript bytes in, conversational turns out.

Each adapter consumes the *new* bytes of one source file (from the
persisted offset) and returns an :class:`ExtractResult`:

- ``turns`` — the conversational turns found;
- ``consumed`` — how many of the given bytes were consumed. Only
  complete, newline-terminated lines are consumed; a partial tail is
  left for the next pass;
- ``errors`` — count of malformed/unknown records skipped. Never raises
  on bad input: unknown schemas are counted and skipped, not fatal
  (spec: review round 1, finding 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from smriti.daylog.model import Turn


@dataclass
class ExtractResult:
    turns: list[Turn] = field(default_factory=list)
    consumed: int = 0
    errors: int = 0


class Adapter(Protocol):
    def extract(self, path: Path, blob: bytes) -> ExtractResult: ...


def complete_lines(blob: bytes) -> tuple[list[bytes], int]:
    """Split *blob* into newline-terminated lines; return (lines, consumed)."""
    end = blob.rfind(b"\n")
    if end < 0:
        return [], 0
    consumed = end + 1
    return blob[:consumed].split(b"\n")[:-1], consumed


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

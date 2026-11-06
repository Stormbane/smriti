"""The day-log: a unified, chronological record of user<->agent communication.

Every channel (Claude Code sessions, the Telegram typed-chat brain, box
voice, Codex sessions) already streams transcripts to disk. This package
tails those sources through per-source adapters and merges conversational
turns into one JSONL file per day under ``<tree>/log/YYYY/MM/DD.jsonl``.

Capture is deterministic — no LLM anywhere in this package's collection
path. Guarantees (see .ai/features/nightly-cycle.md):

- at-least-once reads, exactly-once log: every turn carries a stable id
  and all writers dedupe against the day-file;
- single writer: an OS lockfile guards each append burst;
- event time owns placement: a turn lands in the day-file of its parsed
  timestamp (fixed-offset local day), not the day it was captured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from smriti.daylog.model import Turn, day_key, local_day

if TYPE_CHECKING:
    from smriti.daylog.writer import append_turns

__all__ = ["Turn", "day_key", "local_day", "append_turns"]


def __getattr__(name: str) -> Any:
    # The writer (its locks and the scrubber) is imported where it is used, not
    # by every reader of daylog.json: the MCP server reads it on each request.
    if name == "append_turns":
        from smriti.daylog.writer import append_turns

        return append_turns
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

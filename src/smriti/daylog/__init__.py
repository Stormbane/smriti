"""The day-log: a unified, chronological record of Suti<->Narada communication.

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

from smriti.daylog.model import Turn, day_key, local_day
from smriti.daylog.writer import append_turns

__all__ = ["Turn", "day_key", "local_day", "append_turns"]

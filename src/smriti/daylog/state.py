"""Per-source capture state, persisted crash-safe.

For every source file we track identity (first-line fingerprint + last
size), the consumed byte offset, and health counters. Marks may lag the
log (crash between append and state save): re-reads then re-emit turns
whose ids the writer already holds, and dedup absorbs them — that is
the at-least-once/exactly-once contract.

Health is defined by *scan* recency, not append recency, so a
legitimately quiet channel is distinguishable from a stalled tail.

The day-log's own health rides along: how many lines of each day-file
aren't records, as the writer last read it (a torn append leaves one),
and how many torn last lines the writer has closed (H12.6). Both are
written only when there is something to say, so a healthy state file
looks as it did. An entry that can't be read is dropped, and that file
is read again from the start; dedup absorbs the overlap.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_FINGERPRINT_BYTES = 256

# How many skipped records a file's state remembers (the newest).
MAX_SKIPS_KEPT = 10


@dataclass
class FileState:
    offset: int = 0
    size: int = 0
    fingerprint: str = ""
    last_scan: float = 0.0
    last_turn: float = 0.0
    parse_errors: int = 0
    # Above the offset: a record longer than one read starts at the offset,
    # and the bytes up to here hold no newline, so its end is still to come.
    waiting: int = 0
    # The records skipped as longer than one read: [offset, bytes, unix time].
    skipped: list[list[int]] = field(default_factory=list)
    # Passes in which the file grew while its offset stayed put; two or more is stuck.
    stalled: int = 0
    # What the file's adapter asked to get back with its next read (ExtractResult.carry).
    carry: list[list[Any]] = field(default_factory=list)


# Saved only when set, so the state of a file that never needed them looks as it did.
_OPTIONAL = {"waiting": 0, "skipped": [], "stalled": 0, "carry": []}

# A file that grew in this many passes without its offset moving is stuck.
STUCK_PASSES = 2


@dataclass
class SourceState:
    files: dict[str, FileState] = field(default_factory=dict)
    last_scan: float = 0.0
    last_turn: float = 0.0
    parse_errors: int = 0


def _file_state(raw: dict[str, Any]) -> FileState:
    """A file's saved state; raises TypeError or ValueError on a value of the wrong kind."""
    return FileState(
        offset=int(raw.get("offset", 0)),
        size=int(raw.get("size", 0)),
        fingerprint=str(raw.get("fingerprint", "")),
        last_scan=float(raw.get("last_scan", 0.0)),
        last_turn=float(raw.get("last_turn", 0.0)),
        parse_errors=int(raw.get("parse_errors", 0)),
        waiting=int(raw.get("waiting", 0)),
        skipped=[[int(x) for x in entry] for entry in raw.get("skipped", [])][-MAX_SKIPS_KEPT:],
        stalled=int(raw.get("stalled", 0)),
        carry=[list(entry) for entry in raw.get("carry", []) if isinstance(entry, list)],
    )


def _file_json(fstate: FileState) -> dict[str, Any]:
    return {k: v for k, v in asdict(fstate).items() if _OPTIONAL.get(k, ...) != v}


def _source_json(source: SourceState) -> dict[str, Any]:
    return {"files": {p: _file_json(f) for p, f in source.files.items()},
            "last_scan": source.last_scan, "last_turn": source.last_turn,
            "parse_errors": source.parse_errors}


def fingerprint_head(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(_FINGERPRINT_BYTES)
    except OSError:
        return ""
    return hashlib.sha1(head).hexdigest()[:16]


class DaylogState:
    """Load/mutate/save the whole capture state atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sources: dict[str, SourceState] = {}
        # Days changed by capture but not yet repaired by a nightly run.
        # Persisted so a past day the daemon changed survives until a
        # nightly consumes it.
        self.dirty_days: set[str] = set()
        # Lines of each day-file that aren't records, as the writer last read it.
        self.corrupt_lines: dict[str, int] = {}
        # How many torn last lines the writer has closed, ever.
        self.torn_repairs = 0

    @classmethod
    def load(cls, path: Path) -> "DaylogState":
        state = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return state
        if not isinstance(raw, dict):
            return state
        dirty = raw.get("dirty_days")
        if isinstance(dirty, list):
            state.dirty_days = {str(d) for d in dirty}
        corrupt = raw.get("corrupt_lines")
        if isinstance(corrupt, dict):
            state.corrupt_lines = {str(day): n for day, n in corrupt.items()
                                   if isinstance(n, int) and n > 0}
        repairs = raw.get("torn_repairs")
        state.torn_repairs = repairs if isinstance(repairs, int) else 0
        sources = raw.get("sources")
        for name, src in (sources.items() if isinstance(sources, dict) else ()):
            if not isinstance(src, dict):
                continue
            try:
                source = SourceState(
                    last_scan=float(src.get("last_scan", 0.0)),
                    last_turn=float(src.get("last_turn", 0.0)),
                    parse_errors=int(src.get("parse_errors", 0)),
                )
            except (TypeError, ValueError):
                source = SourceState()
            files = src.get("files")
            for fpath, fstate in (files.items() if isinstance(files, dict) else ()):
                if isinstance(fstate, dict):
                    try:
                        source.files[fpath] = _file_state(fstate)
                    except (TypeError, ValueError):
                        pass  # read again from the start; dedup absorbs it
            state.sources[name] = source
        return state

    def source(self, name: str) -> SourceState:
        return self.sources.setdefault(name, SourceState())

    def file(self, source: str, path: Path) -> FileState:
        return self.source(source).files.setdefault(str(path), FileState())

    def note_scan(self, source: str) -> None:
        self.source(source).last_scan = time.time()

    def mark_dirty(self, days: Iterable[str | date]) -> None:
        from smriti.daylog.model import day_key as _day_key

        for day in days:
            self.dirty_days.add(day if isinstance(day, str) else _day_key(day))

    def note_days(self, invalid: dict[str, int], repaired: Iterable[str]) -> None:
        """Record what the writer saw of the day-files it read: their invalid lines and repairs."""
        for day, count in invalid.items():
            if count > 0:
                self.corrupt_lines[day] = count
            else:
                self.corrupt_lines.pop(day, None)
        self.torn_repairs += sum(1 for _ in repaired)

    def save(self) -> None:
        payload: dict[str, object] = {
            "sources": {n: _source_json(s) for n, s in self.sources.items()},
            "dirty_days": sorted(self.dirty_days),
        }
        if self.corrupt_lines:
            payload["corrupt_lines"] = dict(sorted(self.corrupt_lines.items()))
        if self.torn_repairs:
            payload["torn_repairs"] = self.torn_repairs
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # PID-unique temp name: concurrent savers (daemon + nightly, when
        # not lock-serialized, e.g. crash recovery) must never collide on
        # the same temp path.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def health(self) -> dict[str, dict[str, float | int]]:
        """Per-source health summary for `smriti status` / wake briefing."""
        now = time.time()
        out: dict[str, dict[str, float | int]] = {}
        for name, src in self.sources.items():
            out[name] = {
                "scan_age_s": round(now - src.last_scan, 1) if src.last_scan else -1,
                "turn_age_s": round(now - src.last_turn, 1) if src.last_turn else -1,
                "parse_errors": src.parse_errors,
                "files": len(src.files),
            }
        return out

"""Append turns to day-files with dedup, under the writer lock.

Crash safety comes from dedup, not transactions: the capture state may
lag the log, and re-read ranges re-derive identical turn ids that the
writer drops against the day-file's existing ids. Writers never rewrite
a day-file — ordering is the reader's job (render sorts by timestamp).

A torn last line: an append that didn't finish (the process killed, the
power cut) can leave a day-file ending in part of a record. Before
appending, the writer looks at whether the file ends in a newline and
writes one first when it doesn't, so the fragment becomes a line of its
own, which readers skip as invalid, and the records after it stay whole
(glued onto the fragment, the next record would be lost with it). The
torn turn itself comes back: the capture state that
covers it is saved only after the append (see :mod:`smriti.daylog.collect`),
so the next pass reads it again, and its id isn't among the ids the
day-file holds.

Appended records reach the disk (``fsync``) before the capture state
that covers them is saved, so a crash of the machine, not only of the
process, can't leave offsets past turns the day-log lacks. A capture
pass reads each day-file once and keeps what it found in an
:class:`AppendPass`, adding the ids it appends, which is exact because
only the holder of the writer lock writes day-files; it flushes the files
it touched once, at the end of the pass.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import BinaryIO

from smriti.daylog.config import DaylogConfig
from smriti.daylog.lock import writer_lock
from smriti.daylog.model import Turn, day_key
from smriti.core.scrub import scrub

log = logging.getLogger(__name__)


@dataclass
class DayScan:
    """What one read of a day-file found."""

    ids: set[str] = field(default_factory=set)
    invalid: int = 0  # non-blank lines that aren't a JSON object with a string "id"
    torn: bool = False  # it doesn't end in a newline: its last record was cut short


@dataclass
class AppendPass:
    """What one capture pass knows of the day-files it appends to.

    Used under the writer lock, for one pass: each day-file is read once,
    and the files written are flushed by :meth:`sync` before the state
    that covers them is saved.
    """

    scans: dict[str, DayScan] = field(default_factory=dict)  # by day
    repaired: list[str] = field(default_factory=list)  # days whose torn last line was closed
    unsynced: set[Path] = field(default_factory=set)

    @property
    def invalid(self) -> dict[str, int]:
        """Each day-file's lines that aren't records, as last read."""
        return {day: scan.invalid for day, scan in self.scans.items()}

    def sync(self) -> None:
        """Flush every day-file this pass wrote to disk."""
        for path in sorted(self.unsynced):
            with path.open("ab") as f:
                os.fsync(f.fileno())
        self.unsynced.clear()


def scan_day(path: Path) -> DayScan:
    """Read a day-file's ids, and count the lines that aren't records."""
    scan = DayScan()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            line = ""
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip():
                        scan.invalid += 1
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                    scan.ids.add(obj["id"])
                else:
                    scan.invalid += 1
            scan.torn = bool(line) and not line.endswith("\n")
    except OSError:
        pass
    return scan


def load_day_ids(path: Path) -> set[str]:
    return scan_day(path).ids


@dataclass
class DayTurns:
    """What one read of a day-file's turns found."""

    turns: list[Turn] = field(default_factory=list)  # deduped by id, sorted by event time
    invalid: int = 0  # non-blank lines that aren't a turn, skipped
    # False when the file couldn't be read to its end: the turns are what was read before.
    complete: bool = True


def read_day(path: Path) -> DayTurns:
    """Read a day-file's turns, and count the lines skipped as not being one.

    A line is skipped when it isn't JSON, isn't an object, or has no valid
    time or no text; the count is what capture's :attr:`DayScan.invalid`
    counts, plus objects that have an id but aren't a turn. Blank lines and a
    turn repeated by id aren't counted.

    A missing file has no turns, which is a whole answer. Any other OSError
    (a file held open elsewhere, a permission, a failing disk) leaves
    ``complete`` False, so a caller that remembers what a file held knows
    not to remember this read.
    """
    turns: dict[str, Turn] = {}
    invalid = 0
    complete = True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip():
                        invalid += 1
                    continue
                turn = Turn.from_json(obj) if isinstance(obj, dict) else None
                if turn is None:
                    invalid += 1
                elif turn.id not in turns:
                    turns[turn.id] = turn
    except FileNotFoundError:
        pass
    except OSError:
        complete = False
    return DayTurns(sorted(turns.values(), key=lambda t: (t.ts, t.id)), invalid, complete)


def read_day_turns(path: Path) -> list[Turn]:
    """Read a day-file's turns, deduped by id, sorted by event time.

    Lines that aren't turns are skipped; :func:`read_day` also counts them.
    """
    return read_day(path).turns


def _append_bytes(f: BinaryIO, data: bytes) -> None:
    """Write *data* at the end of the open day-file."""
    f.write(data)


def append_turns_unlocked(cfg: DaylogConfig, turns: list[Turn],
                          batch: AppendPass | None = None) -> tuple[int, set[date]]:
    """Append *turns* to their day-files (event-time placement).

    The caller MUST hold the writer lock. Returns ``(appended_count,
    changed_days)`` — changed days drive the nightly repair pass
    regardless of age. Without
    *batch*, each day-file is read for this call and flushed before it
    returns; with one, the pass's reading is reused, and the caller
    flushes with ``batch.sync()`` before it saves any offset.
    """
    if not turns:
        return 0, set()
    by_day: dict[date, list[Turn]] = {}
    for turn in turns:
        by_day.setdefault(turn.day(cfg.root), []).append(turn)

    current = batch if batch is not None else AppendPass()
    appended = 0
    changed: set[date] = set()
    for day, day_turns in sorted(by_day.items()):
        key = day_key(day)
        path = cfg.day_jsonl(key)
        scan = current.scans.get(key)
        if scan is None:
            scan = current.scans[key] = scan_day(path)
        records: list[bytes] = []
        for turn in day_turns:
            if turn.id in scan.ids:
                continue
            scan.ids.add(turn.id)  # the same turn twice in one batch is written once
            record = turn.to_json()
            record["text"] = scrub(record["text"])
            records.append((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
        if not records:
            continue
        # A torn last line is closed first: the fragment becomes a line of its
        # own, which readers skip, and the records after it stay whole.
        head = b"\n" if scan.torn else b""
        if scan.torn:
            log.warning("day-log %s ended in a torn record; closed it before appending", path)
            current.repaired.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as f:
            _append_bytes(f, head + b"".join(records))
        scan.torn = False
        current.unsynced.add(path)
        appended += len(records)
        changed.add(day)
    if batch is None:
        current.sync()
    return appended, changed


def append_turns(cfg: DaylogConfig, turns: list[Turn]) -> tuple[int, set[date]]:
    """Locked wrapper around :func:`append_turns_unlocked`."""
    if not turns:
        return 0, set()
    with writer_lock(cfg.lock_path):
        return append_turns_unlocked(cfg, turns)

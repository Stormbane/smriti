"""One collection pass over all configured sources.

Shared by the logd daemon (frequent small passes) and the nightly
reconcile sweep (backstop). Discovers files by glob, tails each from
its persisted offset through the source's adapter, appends the
extracted turns, then persists state.

Concurrency: the ENTIRE pass runs under the writer
lock, and the state is re-loaded from disk inside the lock — so a logd
pass and the nightly reconcile can interleave but never race a
read-modify-write, clobber each other's saves, or regress offsets.

File identity: a stored head-fingerprint + size. A size regression or
fingerprint change means the path was truncated or reused — re-read
from zero; dedup by turn id absorbs any overlap.

Persistence order, which is why a crash at any point loses no turn:

1. Under the writer lock, the state is loaded from disk.
2. For each file, its new bytes are read from the saved offset, the
   adapter extracts the turns of the complete lines, and the writer
   appends those whose ids the day-file doesn't hold, closing a torn
   last line first. The new offset is kept in memory only.
3. When every file is done, the day-files written are flushed to disk,
   and then the state (offsets, counters, dirty days) is saved by an
   atomic replace.

A crash before step 3 leaves the old offsets on disk, so the next pass
reads the same bytes again and the writer drops every turn whose id the
day-file holds; a turn whose record was torn isn't held (its fragment
doesn't parse), so it is appended again, whole. A crash in step 3 leaves
the old state or the new one, never a mix. The saved offsets never run
ahead of the day-log, so replay plus dedup by turn id recovers every
turn. A state file that can't be read counts as a fresh one: everything
is read again from the start, and dedup absorbs it.

A record longer than one read (``_MAX_CHUNK``; a pasted image is one
base64 line) is skipped (H12.6). A whole read without a newline makes
the pass look for the record's end, ``_SCAN_CHUNK`` at a time, holding
no more than that in memory; at its newline the offset moves past it,
and the skip counts as a parse error, is logged, and is recorded in the
file's state. Until that newline is written the record is waited for,
as a partial line is, and the file's state says so
(``FileState.waiting``); a later pass looks only at the bytes that came
since. Before, a pass read the same 8 MiB again forever, and every later
turn in the file was lost without an error.

A stuck source is visible: a file that grew in two or more passes while
its offset didn't move (``FileState.stalled``; any cause, a record
waited for, line endings an adapter can't split, a bug) is logged when
it becomes stuck, and ``smriti status`` names it, with the records
skipped and the ones waited for.
"""

from __future__ import annotations

import glob as globmod
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from smriti.daylog.adapters import Adapter, get_adapter, is_internal
from smriti.daylog.config import DaylogConfig, SourceSpec
from smriti.daylog.lock import writer_lock
from smriti.daylog.state import (
    MAX_SKIPS_KEPT,
    STUCK_PASSES,
    DaylogState,
    FileState,
    fingerprint_head,
)
from smriti.daylog.writer import AppendPass, append_turns_unlocked

log = logging.getLogger(__name__)

# Read at most this much new data from one file per pass; huge backlogs
# drain over successive passes instead of ballooning memory.
_MAX_CHUNK = 8 * 1024 * 1024
# How much of a record longer than that a pass reads at a time, looking for its end.
_SCAN_CHUNK = 1024 * 1024


@dataclass
class Skipped:
    """A record skipped as longer than one read."""

    path: str
    offset: int
    size: int


@dataclass
class CollectReport:
    appended: int = 0
    changed_days: set[date] = field(default_factory=set)
    files_scanned: int = 0
    parse_errors: int = 0
    repaired_days: list[str] = field(default_factory=list)  # day-files whose torn line was closed
    skipped: list[Skipped] = field(default_factory=list)


def collect_once(cfg: DaylogConfig, *, lock_timeout_s: float = 60.0) -> CollectReport:
    """Run one pass: scan sources, extract new turns, append, save state.

    Loads the persisted state inside the writer lock; the caller never
    owns state across passes (a stale in-memory copy is exactly the
    race this design removes).
    """
    with writer_lock(cfg.lock_path, timeout_s=lock_timeout_s):
        state = DaylogState.load(cfg.state_path)
        batch = AppendPass()
        report = _collect_locked(cfg, state, batch)
        batch.sync()  # the day-log on disk before the offsets that cover it
        # Persist dirty-day markers, so a past day the daemon changed is
        # repaired by the next nightly, which consumes and clears them.
        state.mark_dirty(report.changed_days)
        state.note_days(batch.invalid, batch.repaired)
        report.repaired_days = batch.repaired
        state.save()
    return report


def rescan_source(cfg: DaylogConfig, name: str, *, lock_timeout_s: float = 60.0) -> int:
    """Read every file of source *name* again from its start; returns how many files.

    Only the capture state changes: each file's offset goes back to 0, so
    the next pass reads it all again, and the writer drops every turn the
    day-log already holds. It is how turns that an older adapter couldn't
    read are backfilled (H12.17).
    """
    with writer_lock(cfg.lock_path, timeout_s=lock_timeout_s):
        state = DaylogState.load(cfg.state_path)
        files = state.sources[name].files if name in state.sources else {}
        for fstate in files.values():
            fstate.offset = fstate.waiting = fstate.stalled = 0
            fstate.carry = []
        state.save()
    return len(files)


def _collect_locked(cfg: DaylogConfig, state: DaylogState, batch: AppendPass) -> CollectReport:
    report = CollectReport()
    for spec in cfg.sources:
        state.note_scan(spec.name)
        try:
            adapter = get_adapter(spec.kind)
        except ValueError:
            state.source(spec.name).parse_errors += 1
            continue
        for raw_path in sorted(globmod.glob(spec.glob)):
            if is_internal(raw_path):  # smriti's own LLM calls (see adapters.is_internal)
                continue
            path = Path(raw_path)
            fstate = state.file(spec.name, path)
            fstate.last_scan = time.time()
            report.files_scanned += 1
            try:
                size = path.stat().st_size
            except OSError:
                continue
            fingerprint = fingerprint_head(path)
            if size < fstate.offset or (
                fstate.fingerprint and fingerprint and fingerprint != fstate.fingerprint
            ):
                # truncated or recreated: re-read, dedup absorbs
                fstate.offset = fstate.waiting = fstate.stalled = 0
                fstate.carry = []
            elif size < fstate.size or fstate.waiting > size:
                # It shrank but kept what was read: what lay past the offset was
                # rewritten, so a long record waited for there may be gone. Look again.
                fstate.waiting = fstate.stalled = 0
            grew, start = size > fstate.size, fstate.offset
            fstate.size = size
            fstate.fingerprint = fingerprint
            _read_new(cfg, state, spec, adapter, path, fstate, report, batch)
            _note_progress(path, fstate, start, grew)
    return report


def _note_progress(path: Path, fstate: FileState, start: int, grew: bool) -> None:
    """Count the passes in which the file grew while its offset stayed put."""
    if fstate.offset != start or fstate.offset >= fstate.size:
        fstate.stalled = 0
    elif grew:
        fstate.stalled += 1
        if fstate.stalled == STUCK_PASSES and not fstate.waiting:
            log.warning("capture: %s is stuck at byte %d of %d: it grew in %d passes and "
                        "nothing more of it could be read", path, fstate.offset, fstate.size,
                        fstate.stalled)


def _read_new(cfg: DaylogConfig, state: DaylogState, spec: SourceSpec, adapter: Adapter,
              path: Path, fstate: FileState, report: CollectReport, batch: AppendPass) -> None:
    """Capture what is new in one file: one read of complete lines, after at most one skip."""
    skipped = False
    while fstate.size > fstate.offset:
        if fstate.waiting:  # a record longer than one read starts at the offset
            if skipped or fstate.size <= fstate.waiting:
                return  # nothing new since the last look: its end is still to come
            if not _skip_record(state, spec, path, fstate, report):
                return
            skipped = True
            continue
        try:
            with path.open("rb") as f:
                f.seek(fstate.offset)
                blob = f.read(_MAX_CHUNK)
        except OSError:
            return
        if len(blob) >= _MAX_CHUNK and b"\n" not in blob:
            fstate.waiting = fstate.offset + len(blob)
            blob = b""  # held no longer than the look: memory stays one read
            continue
        result = adapter.extract(path, blob, fstate.carry)
        if result.consumed <= 0 and not result.errors:
            return  # a partial last line, left for the next pass
        if result.carry is not None:  # saved with the offset it goes with
            fstate.carry = result.carry
        appended, changed = append_turns_unlocked(cfg, result.turns, batch)
        report.appended += appended
        report.changed_days |= changed
        fstate.offset += result.consumed
        _count_errors(state, spec, fstate, report, result.errors)
        if result.turns:
            now = time.time()
            fstate.last_turn = now
            state.source(spec.name).last_turn = now
        return


def _skip_record(state: DaylogState, spec: SourceSpec, path: Path, fstate: FileState,
                 report: CollectReport) -> bool:
    """Skip the over-long record at the offset if its end has been written; True if it was.

    Looks only at the bytes after ``waiting``, which earlier looks found
    no newline in; when there is none yet, ``waiting`` moves to the end.
    """
    try:
        end = _next_line_start(path, max(fstate.waiting, fstate.offset), fstate.size)
    except OSError:
        return False
    if end is None:
        fstate.waiting = fstate.size
        return False
    skip = Skipped(str(path), fstate.offset, end - fstate.offset)
    log.warning("capture: skipped a %.1f MiB record at byte %d of %s; it is longer than "
                "one read (%d MiB)", skip.size / 2**20, skip.offset, path, _MAX_CHUNK // 2**20)
    fstate.skipped = [*fstate.skipped, [skip.offset, skip.size, int(time.time())]]
    del fstate.skipped[:-MAX_SKIPS_KEPT]
    fstate.offset, fstate.waiting = end, 0
    report.skipped.append(skip)
    _count_errors(state, spec, fstate, report, 1)
    return True


def _next_line_start(path: Path, start: int, stop: int) -> int | None:
    """Where the line after the first newline in ``[start, stop)`` begins; None without one."""
    with path.open("rb") as f:
        f.seek(start)
        pos = start
        while pos < stop:
            chunk = f.read(min(_SCAN_CHUNK, stop - pos))
            if not chunk:
                return None
            newline = chunk.find(b"\n")
            if newline >= 0:
                return pos + newline + 1
            pos += len(chunk)
    return None


def _count_errors(state: DaylogState, spec: SourceSpec, fstate: FileState,
                  report: CollectReport, errors: int) -> None:
    fstate.parse_errors += errors
    state.source(spec.name).parse_errors += errors
    report.parse_errors += errors

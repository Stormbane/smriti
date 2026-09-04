"""One collection pass over all configured sources.

Shared by the logd daemon (frequent small passes) and the nightly
reconcile sweep (backstop). Discovers files by glob, tails each from
its persisted offset through the source's adapter, appends the
extracted turns, then persists state.

Concurrency (diff review P2): the ENTIRE pass runs under the writer
lock, and the state is re-loaded from disk inside the lock — so a logd
pass and the nightly reconcile can interleave but never race a
read-modify-write, clobber each other's saves, or regress offsets.

File identity: a stored head-fingerprint + size. A size regression or
fingerprint change means the path was truncated or reused — re-read
from zero; dedup by turn id absorbs any overlap.
"""

from __future__ import annotations

import glob as globmod
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from smriti.daylog.adapters import get_adapter
from smriti.daylog.config import DaylogConfig
from smriti.daylog.lock import writer_lock
from smriti.daylog.state import DaylogState, fingerprint_head
from smriti.daylog.writer import append_turns_unlocked

# Read at most this much new data from one file per pass; huge backlogs
# drain over successive passes instead of ballooning memory.
_MAX_CHUNK = 8 * 1024 * 1024

# Transcript namespaces produced by smriti's OWN internal LLM
# subprocesses (see smriti.llm.workdir) — never captured, or derived
# text would feed back into future digests.
_EXCLUDE_SUBSTRINGS = ("llm-workdir",)


@dataclass
class CollectReport:
    appended: int = 0
    changed_days: set[date] = field(default_factory=set)
    files_scanned: int = 0
    parse_errors: int = 0


def collect_once(cfg: DaylogConfig, *, lock_timeout_s: float = 60.0) -> CollectReport:
    """Run one pass: scan sources, extract new turns, append, save state.

    Loads the persisted state inside the writer lock; the caller never
    owns state across passes (a stale in-memory copy is exactly the
    race this design removes).
    """
    with writer_lock(cfg.lock_path, timeout_s=lock_timeout_s):
        state = DaylogState.load(cfg.state_path)
        report = _collect_locked(cfg, state)
        # Persist dirty-day markers so historical changes captured by the
        # daemon survive until the next nightly repair pass (diff review
        # P1) — nightly consumes and clears them.
        state.mark_dirty(report.changed_days)
        state.save()
    return report


def _collect_locked(cfg: DaylogConfig, state: DaylogState) -> CollectReport:
    report = CollectReport()
    for spec in cfg.sources:
        state.note_scan(spec.name)
        try:
            adapter = get_adapter(spec.kind)
        except ValueError:
            state.source(spec.name).parse_errors += 1
            continue
        for raw_path in sorted(globmod.glob(spec.glob)):
            if any(marker in raw_path for marker in _EXCLUDE_SUBSTRINGS):
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
                fstate.offset = 0  # truncated or recreated: re-read, dedup absorbs
            fstate.size = size
            fstate.fingerprint = fingerprint
            if size <= fstate.offset:
                continue
            try:
                with path.open("rb") as f:
                    f.seek(fstate.offset)
                    blob = f.read(_MAX_CHUNK)
            except OSError:
                continue
            result = adapter.extract(path, blob)
            if result.consumed <= 0 and not result.errors:
                continue
            appended, changed = append_turns_unlocked(cfg, result.turns)
            report.appended += appended
            report.changed_days |= changed
            fstate.offset += result.consumed
            fstate.parse_errors += result.errors
            state.source(spec.name).parse_errors += result.errors
            report.parse_errors += result.errors
            if result.turns:
                now = time.time()
                fstate.last_turn = now
                state.source(spec.name).last_turn = now
    return report

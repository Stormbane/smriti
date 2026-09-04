"""One collection pass over all configured sources.

Shared by the logd daemon (frequent small passes) and the nightly
reconcile sweep (backstop). Discovers files by glob, tails each from
its persisted offset through the source's adapter, appends the
extracted turns, then persists state. State saves AFTER appends, so a
crash between the two re-reads a range — dedup absorbs it.

File identity: a stored head-fingerprint + size. A size regression or
fingerprint change means the path was truncated or reused — re-read
from zero (spec: review round 1, finding 1).
"""

from __future__ import annotations

import glob as globmod
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from smriti.daylog.adapters import get_adapter
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn
from smriti.daylog.state import DaylogState, fingerprint_head
from smriti.daylog.writer import append_turns

# Read at most this much new data from one file per pass; huge backlogs
# drain over successive passes instead of ballooning memory.
_MAX_CHUNK = 8 * 1024 * 1024


@dataclass
class CollectReport:
    appended: int = 0
    changed_days: set[date] = field(default_factory=set)
    files_scanned: int = 0
    parse_errors: int = 0


def collect_once(cfg: DaylogConfig, state: DaylogState) -> CollectReport:
    """Run one pass: scan sources, extract new turns, append, save state."""
    report = CollectReport()
    pending: list[Turn] = []
    touched: list[tuple[str, str, int, int, int]] = []  # (source, file, new_offset, turns, errors)

    for spec in cfg.sources:
        state.note_scan(spec.name)
        try:
            adapter = get_adapter(spec.kind)
        except ValueError:
            state.source(spec.name).parse_errors += 1
            continue
        for raw_path in sorted(globmod.glob(spec.glob)):
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
            pending.extend(result.turns)
            touched.append(
                (spec.name, str(path), fstate.offset + result.consumed,
                 len(result.turns), result.errors)
            )

    appended, changed = append_turns(cfg, pending)
    report.appended = appended
    report.changed_days = changed

    # Advance marks only after the append landed.
    now = time.time()
    for source, fpath, new_offset, turn_count, errors in touched:
        src = state.source(source)
        fstate = src.files[fpath]
        fstate.offset = new_offset
        fstate.parse_errors += errors
        src.parse_errors += errors
        report.parse_errors += errors
        if turn_count:
            fstate.last_turn = now
            src.last_turn = now
    state.save()
    return report

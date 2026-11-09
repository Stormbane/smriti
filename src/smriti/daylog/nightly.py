"""``smriti nightly`` — the sleep task (~03:00).

Steps (spec Design 4): reconcile sweep -> repair pass -> render ->
digest -> rollup scan (capped) -> reindex -> witness -> status file. Idempotent by
construction: every step derives from inputs carrying an input hash;
re-running a completed night is a no-op. Failure of any step is
recorded in the status file and never blocks the remaining steps —
silence must be visible, not fatal.

One run at a time per memory root (:func:`run_lock_key`): the scheduled run
and a manual one never interleave their writes, and the status file is the
last complete run's.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from smriti.core.files import write_atomic
from smriti.core.oslock import BUSY, UNAVAILABLE, LockKey, lock_path, try_lock
from smriti.core.scrub import scrub_json
from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.digest import digest_day, digest_fingerprint
from smriti.daylog.model import day_key, day_tz
from smriti.daylog.render import (
    content_hash,
    head_field,
    head_is_of,
    input_hash,
    read_head,
    render_day,
    render_snapshot,
)
from smriti.daylog.rollup import build_rollup, enumerate_stale_periods, orphaned_rollups
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import DayUnreadable, read_day

log = logging.getLogger(__name__)

_REPAIR_WINDOW_DAYS = 7
# How long a second run waits for the first to finish before it skips. A run
# with digests takes minutes, so this only covers one that is just finishing.
_LOCK_WAIT_S = 10.0
# Why a run that can't open its lock file skips; the wake shows it.
_LOCK_UNAVAILABLE = ("the run lock can't be taken (no lock file could be opened: check the "
                     "runtime directory, SMRITI_RUNTIME_DIR), so the night was skipped "
                     "rather than run without it")


def _target_day(now: datetime, root: Path | None = None) -> date:
    """The just-closed local day (the run happens after its 03:00 cutoff)."""
    return (now.astimezone(day_tz(root)) - timedelta(days=1)).date()


_YEAR_RE = re.compile(r"[0-9]{4}")
_MONTH_RE = re.compile(r"[0-9]{2}")
_DAY_FILE_RE = re.compile(r"([0-9]{2})\.jsonl")


def _subdirs(path: Path | str, name: re.Pattern[str]) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as entries:
            return [e for e in entries if name.fullmatch(e.name) and e.is_dir()]
    except OSError:
        return []


def _log_day_files(cfg: DaylogConfig) -> dict[str, tuple[int, int]]:
    """Every day-file in the tree (``log/YYYY/MM/DD.jsonl``) by day: its size and mtime (ns).

    From the directory listing, which costs no open per file (on Windows) and
    is taken before any file is read: a listing that lags a write in progress
    records an older stat with the newer content, which only makes the next
    night read that file again.
    """
    files: dict[str, tuple[int, int]] = {}
    for year in _subdirs(cfg.log_dir, _YEAR_RE):
        for month in _subdirs(year.path, _MONTH_RE):
            try:
                with os.scandir(month.path) as entries:
                    for entry in entries:
                        m = _DAY_FILE_RE.fullmatch(entry.name)
                        if m and entry.is_file():
                            st = entry.stat()
                            files[f"{year.name}-{month.name}-{m.group(1)}"] = (
                                st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return files


# The scan's marker: what each day-file held when the nightly last read it.
_MARKER_FILE = "nightly-scan.json"
# Bump when what an entry caches changes meaning (the input hash, what counts
# as a corrupt line): a marker of another version is ignored, and the night
# scans in full. 2: each entry has its turns' content hash too.
_MARKER_VERSION = 2
# A full scan at least this often, whatever the marker holds: the safety net
# for a day-file changed in a way its size and mtime don't show.
_FULL_SCAN_EVERY = timedelta(days=7)


@dataclass(frozen=True)
class _Checked:
    """A day-file as the scan last read it.

    Its size and mtime are from before the read, so a change during or
    after the read makes the next night's differ, and it is read again.
    """

    size: int
    mtime_ns: int
    hash: str  # its turns' input hash; "" when it had none
    corrupt: int  # its lines that aren't turns
    content: str  # its turns' content hash; "" when it had none


@dataclass
class _Marker:
    full_scan: date  # the target day of the last full scan
    days: dict[str, _Checked]


def _marker_path(cfg: DaylogConfig) -> Path:
    return cfg.root / ".smriti" / _MARKER_FILE


def _load_marker(path: Path) -> _Marker | None:
    """The marker, or None when it is missing, unreadable or of another version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != _MARKER_VERSION:
            return None
        days: dict[str, _Checked] = {}
        for day, entry in raw["days"].items():
            size, mtime_ns, digest, corrupt, content = entry
            ints = (size, mtime_ns, corrupt)
            if not (all(type(x) is int for x in ints) and isinstance(digest, str)
                    and isinstance(content, str)):
                return None
            days[str(day)] = _Checked(size, mtime_ns, digest, corrupt, content)
        return _Marker(date.fromisoformat(raw["full_scan"]), days)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _save_marker(path: Path, marker: _Marker) -> None:
    payload = {
        "version": _MARKER_VERSION,
        "full_scan": marker.full_scan.isoformat(),
        "days": {day: [c.size, c.mtime_ns, c.hash, c.corrupt, c.content]
                 for day, c in sorted(marker.days.items())},
    }
    try:
        write_atomic(path, json.dumps(payload, separators=(",", ":")))
    except OSError as exc:
        # The night's scan stands; the next one finds the old marker or none.
        log.warning("could not save the nightly's scan marker %s: %s", path, exc)


@dataclass
class _Scan:
    """What the stale-day scan found."""

    stale: list[str] = field(default_factory=list)  # days to render and digest, oldest first
    corrupt: dict[str, int] = field(default_factory=dict)  # day -> its lines that aren't turns
    days: int = 0  # day-files in the tree
    read: int = 0  # day-files read tonight
    full: bool = False  # every day-file read, the marker not trusted
    unreadable: list[str] = field(default_factory=list)  # days whose file couldn't be read
    # Days whose day-file holds nothing, with a render or digest left from turns now gone.
    emptied: list[str] = field(default_factory=list)


def _scan_days(cfg: DaylogConfig, changed: set[date], dirty: set[str],
               target: date) -> _Scan:
    """Days needing render/digest, and the day-files' lines that aren't turns.

    Every day-file in the tree is a candidate, as are the reconcile-changed
    dates (any age), the persisted dirty-day markers from daemon passes (a
    past day logd changed before this run is repaired, not lost) and the
    7-day window. Each candidate's render and digest heads are compared with
    its turns' input hash and content hash (and the digest's fingerprint)
    every night, so no drift survives a night unnoticed.

    A day-file that holds nothing (no turn, and no line that isn't one) but
    has a render or digest is ``emptied``: its turns were removed, and the
    nightly removes those artifacts. A missing day-file is not taken as
    emptied: a tree restored without its day-files (git-ignored), or one
    whose old day-files were pruned, keeps its renders and digests.

    The signalled days (changed, dirty, the window) are read every night.
    Any other day-file is read only when its size or mtime differs from the
    marker's (``.smriti/nightly-scan.json``), which records what each file
    held when it was last read; otherwise the marker's hash and corrupt-line
    count stand for it. A missing, unreadable or old-version marker, or one
    whose last full scan is a week old, means every file is read, so the
    result is the full scan's. A read that fails isn't recorded, so the next
    night reads that file again; the scan names it (``unreadable``), and
    doesn't judge it stale from the turns read before the failure.
    """
    files = _log_day_files(cfg)
    signalled = {day_key(d) for d in changed} | set(dirty)
    for back in range(_REPAIR_WINDOW_DAYS):
        signalled.add(day_key(target - timedelta(days=back)))
    path = _marker_path(cfg)
    marker = _load_marker(path)
    full = marker is None or not (
        timedelta(0) <= target - marker.full_scan < _FULL_SCAN_EVERY)
    known = {} if marker is None or full else marker.days
    fingerprint = digest_fingerprint(cfg)
    scan = _Scan(days=len(files), full=full)
    checked: dict[str, _Checked] = {}
    for day in sorted(signalled | set(files)):
        stat = files.get(day)
        entry = known.get(day)
        if day in signalled or entry is None or stat != (entry.size, entry.mtime_ns):
            read = read_day(cfg.day_jsonl(day))
            scan.read += 1
            current = input_hash(read.turns) if read.turns else ""
            content = content_hash(read.turns) if read.turns else ""
            corrupt = read.invalid
            whole = stat is not None and read.complete
            if not read.complete:
                log.warning("nightly: could not read %s; it is read again next night",
                            cfg.day_jsonl(day))
                scan.unreadable.append(day)
                # A prefix's hash says nothing about the day: it isn't judged
                # tonight, so its render and digest stay as they are.
                current = ""
            # Recorded with the stat taken before this read; not recorded when the
            # file isn't listed, or the read failed (its stat would stand for turns
            # never read).
            entry = (_Checked(stat[0], stat[1], current, corrupt, content)
                     if stat is not None and read.complete else None)
        else:
            current, corrupt, content = entry.hash, entry.corrupt, entry.content
            whole = True  # listed, and unchanged since a whole read
        if entry is not None:
            checked[day] = entry
        if corrupt:
            scan.corrupt[day] = corrupt
        if current:
            if not _day_is_current(cfg, day, current, fingerprint, content):
                scan.stale.append(day)
        elif whole and not corrupt and _has_derived(cfg, day):
            scan.emptied.append(day)
    new = _Marker(target if full or marker is None else marker.full_scan, checked)
    if marker is None or new != marker:
        _save_marker(path, new)
    return scan


def _stale_days(cfg: DaylogConfig, changed: set[date], dirty: set[str],
                target: date) -> list[str]:
    """Days needing render/digest (:func:`_scan_days`)."""
    return _scan_days(cfg, changed, dirty, target).stale


def _day_is_current(cfg: DaylogConfig, day: str, current: str, fingerprint: str,
                    content: str) -> bool:
    """Whether *day*'s render and digest were made from the turns hashed *current*
    (their ids) and *content* (the turns themselves), and the digest as now.

    An artifact that records no ``content_hash`` was written before artifacts
    carried one, and is judged by its input hash alone (:func:`head_is_of`).
    A digest's ``prompt_fp`` must be *fingerprint*, the digest prompt's and
    config's now; a digest that records none was written before digests
    carried one, and is judged without it. So no day is redone for lacking
    either.
    """
    if not head_is_of(read_head(cfg.day_md(day)), current, content):
        return False
    head = read_head(cfg.day_digest(day))
    if not head_is_of(head, current, content):
        return False
    recorded = head_field(head, "prompt_fp")
    return not recorded or recorded == fingerprint


def _has_derived(cfg: DaylogConfig, day: str) -> bool:
    """Whether *day* has a render or a digest."""
    return cfg.day_md(day).exists() or cfg.day_digest(day).exists()


def _remove_derived(cfg: DaylogConfig, day: str) -> bool:
    """Remove *day*'s render and digest, made from turns that are gone; False if
    one stays (the next night tries again)."""
    removed = True
    for path in (cfg.day_md(day), cfg.day_digest(day)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("nightly: could not remove %s, whose day has no turns: %s", path, exc)
            removed = False
    return removed


def run_lock_key(root: Path) -> LockKey:
    """The lock one nightly run holds from its first step to its status file, per memory root."""
    return lock_path("nightly", os.path.normcase(str(root.resolve())))


def run_nightly(
    cfg: DaylogConfig | None = None,
    *,
    now: datetime | None = None,
    rollup_cap: int = 2,
    digest_cap: int = 10,
    reindex: bool = True,
    witness: bool = True,
    lock_wait_s: float = _LOCK_WAIT_S,
) -> dict[str, object]:
    """Run the sleep task; returns (and persists) the status document.

    One run at a time per memory root: a run that finds another holding the
    run lock waits up to ``lock_wait_s``, then returns a status that says it
    skipped (``"skipped": true``, and ok, since nothing failed) without
    writing the status file, which stays the running night's.

    A run that can't take the lock at all (no lock file could be opened,
    :data:`smriti.core.oslock.UNAVAILABLE`) can't exclude another run either,
    so it does no work: it skips, not ok, and writes a status that says why,
    so the wake names it and the CLI exits nonzero. The night's work waits
    for a run that can take the lock.
    """
    cfg = cfg or load_config()
    now = now or datetime.now(tz=day_tz(cfg.root))
    target = _target_day(now, cfg.root)
    status: dict[str, object] = {
        "started": now.isoformat(),
        "target_day": day_key(target),
        "steps": {},
        "llm_attempts": [],
    }
    with try_lock(run_lock_key(cfg.root), wait_s=lock_wait_s) as lock:
        if lock == BUSY:
            log.warning("nightly skipped: another nightly is running for %s", cfg.root)
            status.update(skipped=True, ok=True, reason="another nightly is running")
            return status
        if lock == UNAVAILABLE:
            # No lock file could be opened (smriti.core.oslock says when), so
            # nothing excludes a second run: two would rewrite the same renders,
            # digests and rollups at once.
            log.error("nightly skipped: the run lock can't be taken for %s", cfg.root)
            status.update(skipped=True, ok=False, run_lock="unavailable",
                          reason=_LOCK_UNAVAILABLE)
            status["finished"] = datetime.now(tz=day_tz(cfg.root)).isoformat()
            _write_status(cfg.nightly_status_path, status)
            return status
        _run_steps(cfg, status, target, rollup_cap=rollup_cap, digest_cap=digest_cap,
                   reindex=reindex, witness=witness)
    return status


def _run_steps(cfg: DaylogConfig, status: dict[str, object], target: date, *,
               rollup_cap: int, digest_cap: int, reindex: bool, witness: bool) -> None:
    """Every step of one night, then the status file; under the run lock.

    The reconcile, witness and backup steps import their modules inside
    their own ``try``, so a module that can't be imported fails that step,
    in the status, and not the night.
    """
    steps: dict[str, object] = status["steps"]  # type: ignore[assignment]
    attempts: list[dict[str, str]] = status["llm_attempts"]  # type: ignore[assignment]

    # 1. Reconcile sweep (backstop for daemon gaps).
    changed: set[date] = set()
    dirty: set[str] = set()
    try:
        from smriti.daylog.collect import collect_once

        report = collect_once(cfg)
        changed = report.changed_days
        dirty = DaylogState.load(cfg.state_path).dirty_days
        steps["reconcile"] = {
            "ok": True,
            "appended": report.appended,
            "files_scanned": report.files_scanned,
            "parse_errors": report.parse_errors,
        }
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["reconcile"] = {"ok": False, "error": str(exc)[:300]}

    # 2+3+4. Repair/render/digest every stale day: reconcile-changed and
    # daemon-marked dirty dates at any age, the 7-day window, and a full
    # hash sweep of the tree.
    rendered: list[str] = []
    digested: list[str] = []
    digest_errors: dict[str, str] = {}
    try:
        scan = _scan_days(cfg, changed, dirty, target)
        stale = scan.stale
        # A day whose turns were all removed loses the render and digest made
        # from them, so their text stops being searchable (the index purges it).
        emptied = [day for day in scan.emptied if _remove_derived(cfg, day)]
        not_removed = sorted(set(scan.emptied) - set(emptied))
        # Render everything (cheap, deterministic); digest newest-first
        # under a per-night cap so a large backfill catches up over a
        # few nights instead of burning the seat in one.
        digest_days = sorted(stale, reverse=True)[:digest_cap]
        to_digest = set(digest_days)
        # A day-file that can't be read to its end publishes nothing: its
        # render and digest stay as they were, and the next night tries again.
        unreadable = list(scan.unreadable)
        for day in stale:
            if day in to_digest:
                continue
            try:
                if render_day(cfg, day) is not None:
                    rendered.append(day)
            except DayUnreadable as exc:
                log.warning("nightly: %s; its render is left as it was", exc)
                unreadable.append(day)
        # A digested day is rendered and digested from one read of its turns,
        # so its digest hashes exactly what it summarised.
        for day in digest_days:
            try:
                snapshot = render_snapshot(cfg, day)
            except DayUnreadable as exc:
                log.warning("nightly: %s; its render and digest are left as they were", exc)
                unreadable.append(day)
                continue
            if snapshot is None:
                continue
            rendered.append(day)
            try:
                if digest_day(cfg, day, attempts, snapshot=snapshot) is not None:
                    digested.append(day)
            except Exception as exc:  # noqa: BLE001 — a failed digest leaves
                # the stale hash in place; the next nightly retries it.
                digest_errors[day] = str(exc)[:300]
            del snapshot  # one day's turns in memory at a time
        rendered.sort()
        render_step: dict[str, object] = {
            "ok": True, "days": rendered,
            "scan": {"days": scan.days, "read": scan.read, "full": scan.full}}
        if scan.corrupt:
            # Lines the day's readers skip (render, digest): counted, so they're seen.
            render_step["corrupt_lines"] = scan.corrupt
        if unreadable:
            # Day-files that couldn't be read tonight, by the scan or the render;
            # nothing was published from them, and the next night reads them again.
            render_step["unreadable"] = sorted(set(unreadable))
        if emptied:
            render_step["emptied"] = emptied
        if not_removed:
            render_step["not_removed"] = not_removed
        steps["render"] = render_step
        # A failed digest is a failed night: the wake briefing must say
        # so, and the CLI must exit nonzero.
        steps["digest"] = {"ok": not digest_errors, "days": digested,
                           "errors": digest_errors,
                           "deferred": max(0, len(stale) - len(digest_days))}
        _clear_dirty(cfg, dirty, {*digest_errors, *unreadable})
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["render"] = {"ok": False, "error": str(exc)[:300]}

    # 5. Rollup scan — all closed stale periods, oldest first, capped.
    try:
        stale_periods = enumerate_stale_periods(cfg, target)
        built: list[str] = []
        for period in stale_periods[:rollup_cap]:
            build_rollup(period, attempts, cfg)
            built.append(str(period.out_path.name))
        rollup_step: dict[str, object] = {
            "ok": True,
            "built": built,
            "remaining": max(0, len(stale_periods) - rollup_cap),
        }
        # Week rollups with no digest left (their days emptied): kept, and named.
        orphaned = [p.relative_to(cfg.log_dir).as_posix() for p in orphaned_rollups(cfg)]
        if orphaned:
            rollup_step["orphaned"] = orphaned
        steps["rollups"] = rollup_step
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["rollups"] = {"ok": False, "error": str(exc)[:300]}

    # 6. Reindex (smriti index, incremental).
    if reindex:
        steps["index"] = _index_with_retries(cfg)

    # 7. Witness: commit the tree and timestamp its HEAD, so a rewritten
    # history can't pass unnoticed.
    if witness:
        try:
            from smriti.witness import run_witness

            steps["witness"] = run_witness(cfg.root, day_key(target))
        except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
            steps["witness"] = {"ok": False, "error": str(exc)[:300]}

    # 8. Backup: the last commit and push the backup log records, whoever made
    # them (this night's witness, the hourly task), so a failing backup fails
    # the night where the wake sees it. A tree with nowhere to push isn't
    # failing.
    try:
        from smriti.backup import backup_status

        steps["backup"] = backup_status(cfg.root).as_dict()
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["backup"] = {"ok": False, "error": str(exc)[:300]}

    # 9. Status file (consumed by the wake briefing).
    status["finished"] = datetime.now(tz=day_tz(cfg.root)).isoformat()
    status["ok"] = all(
        (s.get("ok", True) if isinstance(s, dict) else True) for s in steps.values()
    )
    _write_status(cfg.nightly_status_path, status)


# Retry schedule for the index step: a live session's MCP server can
# hold index.db's write lock for minutes. Waits between attempts ride out
# a transient hold; a truly wedged writer still surfaces as a failed step.
_INDEX_RETRY_DELAYS_S = (0.0, 30.0, 90.0)
# The index step's wait for that lock, longer than an interactive writer's.
# Passed to the indexer, never set in the process environment.
_INDEX_BUSY_TIMEOUT_MS = 60_000


def _index_with_retries(cfg: DaylogConfig) -> dict[str, object]:
    from smriti.store.indexer import index_tree

    last_error = ""
    for attempt, delay in enumerate(_INDEX_RETRY_DELAYS_S, start=1):
        if delay:
            time.sleep(delay)
        try:
            stats = index_tree(root=cfg.root, busy_ms=_INDEX_BUSY_TIMEOUT_MS)
            return {"ok": True, "attempts": attempt,
                    **{k: int(v) for k, v in stats.items()}}
        except Exception as exc:  # noqa: BLE001 — logged, retried, then in the status
            last_error = str(exc)[:300]
            log.warning("index attempt %d failed: %s", attempt, last_error)
    return {"ok": False, "error": last_error,
            "attempts": len(_INDEX_RETRY_DELAYS_S)}


def _clear_dirty(cfg: DaylogConfig, dirty: set[str], failed: set[str]) -> None:
    """Drop repaired dirty-day markers; keep those of *failed* days (a failed
    digest, a day-file that couldn't be read).

    A marker is cleared only when, under the writer lock, the day's
    CURRENT input hash, from a read to the file's end, still matches both
    derived artifacts — so a turn logd appended mid-digest renews the marker
    instead of being silently swallowed for a cycle.
    """
    if not dirty:
        return
    try:
        from smriti.daylog.lock import writer_lock

        fingerprint = digest_fingerprint(cfg)
        with writer_lock(cfg.lock_path, timeout_s=30.0):
            state = DaylogState.load(cfg.state_path)
            for day in dirty:
                if day in failed:
                    continue
                read = read_day(cfg.day_jsonl(day))
                if not read.complete:
                    continue  # not read to its end — stays dirty
                turns = read.turns
                if turns and not _day_is_current(cfg, day, input_hash(turns), fingerprint,
                                                 content_hash(turns)):
                    continue  # changed underneath us — stays dirty
                state.dirty_days.discard(day)
            state.save()
    except Exception:  # noqa: BLE001 — markers surviving too long is harmless
        log.warning("could not clear dirty-day markers", exc_info=True)


def _write_status(path: Path, status: dict[str, object]) -> None:
    """Write the status document, every string in it scrubbed first.

    It is under ``log/``, which backups commit and push, and a provider's
    error text (``llm_attempts``, a step's ``error``) can quote a credential.
    write_atomic's temp name is the process's and thread's, so two writers
    never share one.
    """
    write_atomic(path, json.dumps(scrub_json(status), indent=1, default=str))

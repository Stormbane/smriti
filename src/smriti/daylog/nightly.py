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
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from smriti.core.files import write_atomic
from smriti.core.oslock import BUSY, UNAVAILABLE, LockKey, lock_path, try_lock
from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.digest import digest_day, digest_fingerprint
from smriti.daylog.model import Turn, day_key, day_tz
from smriti.daylog.render import (
    head_field,
    input_hash,
    input_hash_of,
    read_head,
    read_input_hash,
    render_day,
    render_snapshot,
)
from smriti.daylog.rollup import build_rollup, enumerate_stale_periods
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import read_day_turns

log = logging.getLogger(__name__)

_REPAIR_WINDOW_DAYS = 7
# How long a second run waits for the first to finish before it skips. A run
# with digests takes minutes, so this only covers one that is just finishing.
_LOCK_WAIT_S = 10.0


def _target_day(now: datetime, root: Path | None = None) -> date:
    """The just-closed local day (the run happens after its 03:00 cutoff)."""
    return (now.astimezone(day_tz(root)) - timedelta(days=1)).date()


def _all_log_days(cfg: DaylogConfig) -> set[str]:
    days: set[str] = set()
    for path in cfg.log_dir.glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9].jsonl"):
        days.add(f"{path.parent.parent.name}-{path.parent.name}-{path.stem}")
    return days


def _stale_days(cfg: DaylogConfig, changed: set[date], dirty: set[str],
                target: date) -> list[str]:
    """Days needing render/digest.

    Candidates: reconcile-changed dates (any age), persisted dirty-day
    markers from daemon passes (a past day logd changed before this run
    is repaired, not lost), the 7-day
    window, and — because staleness is a cheap hash compare — every day
    file in the tree, so no drift can ever survive a night unnoticed.
    """
    candidates = {day_key(d) for d in changed} | set(dirty) | _all_log_days(cfg)
    for back in range(_REPAIR_WINDOW_DAYS):
        candidates.add(day_key(target - timedelta(days=back)))
    fingerprint = digest_fingerprint(cfg)
    stale: list[str] = []
    for day in sorted(candidates):
        turns = read_day_turns(cfg.day_jsonl(day))
        if turns and not _day_is_current(cfg, day, turns, fingerprint):
            stale.append(day)
    return stale


def _day_is_current(cfg: DaylogConfig, day: str, turns: list[Turn], fingerprint: str) -> bool:
    """Whether *day*'s render and digest were made from *turns*, and the digest as now.

    A digest's ``prompt_fp`` (H12.12) must be *fingerprint*, the digest
    prompt's and config's now; a digest written before H12.12 records none,
    and is judged by its input hash alone, so the upgrade redoes no day.
    """
    current = input_hash(turns)
    if read_input_hash(cfg.day_md(day)) != current:
        return False
    head = read_head(cfg.day_digest(day))
    if input_hash_of(head) != current:
        return False
    recorded = head_field(head, "prompt_fp")
    return not recorded or recorded == fingerprint


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
            # No lock file could be opened (smriti.core.oslock says when): run as
            # every nightly did before the lock, and say so.
            log.warning("nightly: the run lock can't be taken; running without it")
            status["run_lock"] = "unavailable"
        _run_steps(cfg, status, target, rollup_cap=rollup_cap, digest_cap=digest_cap,
                   reindex=reindex, witness=witness)
    return status


def _run_steps(cfg: DaylogConfig, status: dict[str, object], target: date, *,
               rollup_cap: int, digest_cap: int, reindex: bool, witness: bool) -> None:
    """Every step of one night, then the status file; under the run lock."""
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
        stale = _stale_days(cfg, changed, dirty, target)
        # Render everything (cheap, deterministic); digest newest-first
        # under a per-night cap so a large backfill catches up over a
        # few nights instead of burning the seat in one.
        digest_days = sorted(stale, reverse=True)[:digest_cap]
        to_digest = set(digest_days)
        for day in stale:
            if day not in to_digest and render_day(cfg, day) is not None:
                rendered.append(day)
        # A digested day is rendered and digested from one read of its turns,
        # so its digest hashes exactly what it summarised (H12.11).
        for day in digest_days:
            snapshot = render_snapshot(cfg, day)
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
        steps["render"] = {"ok": True, "days": rendered}
        # A failed digest is a failed night: the wake briefing must say
        # so, and the CLI must exit nonzero.
        steps["digest"] = {"ok": not digest_errors, "days": digested,
                           "errors": digest_errors,
                           "deferred": max(0, len(stale) - len(digest_days))}
        _clear_dirty(cfg, dirty, digest_errors)
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["render"] = {"ok": False, "error": str(exc)[:300]}

    # 5. Rollup scan — all closed stale periods, oldest first, capped.
    try:
        stale_periods = enumerate_stale_periods(cfg, target)
        built: list[str] = []
        for period in stale_periods[:rollup_cap]:
            build_rollup(period, attempts, cfg)
            built.append(str(period.out_path.name))
        steps["rollups"] = {
            "ok": True,
            "built": built,
            "remaining": max(0, len(stale_periods) - rollup_cap),
        }
    except Exception as exc:  # noqa: BLE001 — recorded in the status; the night goes on
        steps["rollups"] = {"ok": False, "error": str(exc)[:300]}

    # 6. Reindex (smriti index, incremental). No qmd refresh follows: its
    # only consumer, the ambient-recall hook, is off (plan item 0.1), and
    # `qmd embed` could fail without saying so.
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
    # the night where the wake sees it (H12.13). A tree with nowhere to push
    # isn't failing.
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
_INDEX_BUSY_TIMEOUT_MS = "60000"


def _index_with_retries(cfg: DaylogConfig) -> dict[str, object]:
    from smriti.store.indexer import index_tree

    previous = os.environ.get("SMRITI_DB_BUSY_TIMEOUT_MS")
    os.environ["SMRITI_DB_BUSY_TIMEOUT_MS"] = _INDEX_BUSY_TIMEOUT_MS
    try:
        last_error = ""
        for attempt, delay in enumerate(_INDEX_RETRY_DELAYS_S, start=1):
            if delay:
                time.sleep(delay)
            try:
                stats = index_tree(root=cfg.root)
                return {"ok": True, "attempts": attempt,
                        **{k: int(v) for k, v in stats.items()}}
            except Exception as exc:  # noqa: BLE001 — logged, retried, then in the status
                last_error = str(exc)[:300]
                log.warning("index attempt %d failed: %s", attempt, last_error)
        return {"ok": False, "error": last_error,
                "attempts": len(_INDEX_RETRY_DELAYS_S)}
    finally:
        if previous is None:
            os.environ.pop("SMRITI_DB_BUSY_TIMEOUT_MS", None)
        else:
            os.environ["SMRITI_DB_BUSY_TIMEOUT_MS"] = previous


def _clear_dirty(cfg: DaylogConfig, dirty: set[str], digest_errors: dict[str, str]) -> None:
    """Drop repaired dirty-day markers; keep the ones whose digest failed.

    A marker is cleared only when, under the writer lock, the day's
    CURRENT input hash still matches both derived artifacts — so a turn
    logd appended mid-digest renews the marker instead of being silently
    swallowed for a cycle.
    """
    if not dirty:
        return
    try:
        from smriti.daylog.lock import writer_lock

        fingerprint = digest_fingerprint(cfg)
        with writer_lock(cfg.lock_path, timeout_s=30.0):
            state = DaylogState.load(cfg.state_path)
            for day in dirty:
                if day in digest_errors:
                    continue
                turns = read_day_turns(cfg.day_jsonl(day))
                if turns and not _day_is_current(cfg, day, turns, fingerprint):
                    continue  # changed underneath us — stays dirty
                state.dirty_days.discard(day)
            state.save()
    except Exception:  # noqa: BLE001 — markers surviving too long is harmless
        log.warning("could not clear dirty-day markers", exc_info=True)


def _write_status(path: Path, status: dict[str, object]) -> None:
    # A temp name of its own (pid and thread): the name used to be the second,
    # which two writers in the same second shared.
    write_atomic(path, json.dumps(status, indent=1, default=str))

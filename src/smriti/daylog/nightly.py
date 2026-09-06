"""``smriti nightly`` — the sleep task (~03:00).

Steps (spec Design 4): reconcile sweep -> repair pass -> render ->
digest -> rollup scan (capped) -> reindex -> status file. Idempotent by
construction: every step derives from inputs carrying an input hash;
re-running a completed night is a no-op. Failure of any step is
recorded in the status file and never blocks the remaining steps —
silence must be visible, not fatal.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.digest import digest_day
from smriti.daylog.model import day_key, day_tz
from smriti.daylog.render import input_hash, read_input_hash, render_day
from smriti.daylog.rollup import build_rollup, enumerate_stale_periods
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import read_day_turns

log = logging.getLogger(__name__)

_REPAIR_WINDOW_DAYS = 7


def _target_day(now: datetime) -> date:
    """The just-closed local day (the run happens after its 03:00 cutoff)."""
    return (now.astimezone(day_tz()) - timedelta(days=1)).date()


def _all_log_days(cfg: DaylogConfig) -> set[str]:
    days: set[str] = set()
    for path in cfg.log_dir.glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9].jsonl"):
        days.add(f"{path.parent.parent.name}-{path.parent.name}-{path.stem}")
    return days


def _stale_days(cfg: DaylogConfig, changed: set[date], dirty: set[str],
                target: date) -> list[str]:
    """Days needing render/digest.

    Candidates: reconcile-changed dates (any age), persisted dirty-day
    markers from daemon passes (diff review P1 — a historical change
    captured by logd before nightly ran must not be lost), the 7-day
    window, and — because staleness is a cheap hash compare — every day
    file in the tree, so no drift can ever survive a night unnoticed.
    """
    candidates = {day_key(d) for d in changed} | set(dirty) | _all_log_days(cfg)
    for back in range(_REPAIR_WINDOW_DAYS):
        candidates.add(day_key(target - timedelta(days=back)))
    stale: list[str] = []
    for day in sorted(candidates):
        turns = read_day_turns(cfg.day_jsonl(day))
        if not turns:
            continue
        current = input_hash(turns)
        if (read_input_hash(cfg.day_md(day)) != current
                or read_input_hash(cfg.day_digest(day)) != current):
            stale.append(day)
    return stale


def run_nightly(
    cfg: DaylogConfig | None = None,
    *,
    now: datetime | None = None,
    rollup_cap: int = 2,
    digest_cap: int = 10,
    reindex: bool = True,
) -> dict[str, object]:
    """Run the sleep task; returns (and persists) the status document."""
    cfg = cfg or load_config()
    now = now or datetime.now(tz=day_tz())
    target = _target_day(now)
    status: dict[str, object] = {
        "started": now.isoformat(),
        "target_day": day_key(target),
        "steps": {},
        "llm_attempts": [],
    }
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
    except Exception as exc:  # noqa: BLE001
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
        for day in stale:
            if render_day(cfg, day) is not None:
                rendered.append(day)
        digest_days = sorted(stale, reverse=True)[:digest_cap]
        for day in digest_days:
            try:
                if digest_day(cfg, day, attempts) is not None:
                    digested.append(day)
            except Exception as exc:  # noqa: BLE001 — a failed digest leaves
                # the stale hash in place; the next nightly retries it.
                digest_errors[day] = str(exc)[:300]
        steps["render"] = {"ok": True, "days": rendered}
        # A failed digest is a failed night (diff review P1): the wake
        # briefing must say so, and the CLI must exit nonzero.
        steps["digest"] = {"ok": not digest_errors, "days": digested,
                           "errors": digest_errors,
                           "deferred": max(0, len(stale) - len(digest_days))}
        _clear_dirty(cfg, dirty, digest_errors)
    except Exception as exc:  # noqa: BLE001
        steps["render"] = {"ok": False, "error": str(exc)[:300]}

    # 5. Rollup scan — all closed stale periods, oldest first, capped.
    try:
        stale_periods = enumerate_stale_periods(cfg, target)
        built: list[str] = []
        for period in stale_periods[:rollup_cap]:
            build_rollup(period, attempts)
            built.append(str(period.out_path.name))
        steps["rollups"] = {
            "ok": True,
            "built": built,
            "remaining": max(0, len(stale_periods) - rollup_cap),
        }
    except Exception as exc:  # noqa: BLE001
        steps["rollups"] = {"ok": False, "error": str(exc)[:300]}

    # 6. Reindex (smriti index incremental + qmd refresh, best-effort).
    if reindex:
        steps["index"] = _index_with_retries(cfg)
        try:
            from smriti.metrics import get_logger

            from smriti.cli import _refresh_recall_index

            _refresh_recall_index(get_logger())
            steps["recall_refresh"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            steps["recall_refresh"] = {"ok": False, "error": str(exc)[:300]}

    # 7. Status file (consumed by the wake briefing).
    status["finished"] = datetime.now(tz=day_tz()).isoformat()
    status["ok"] = all(
        (s.get("ok", True) if isinstance(s, dict) else True) for s in steps.values()
    )
    _write_status(cfg.nightly_status_path, status)
    return status


# Retry schedule for the index step: a live session's MCP server can
# hold index.db's write lock for minutes (first live nightly, 2026-09-06,
# failed on exactly this). Waits between attempts ride out a transient
# hold; a truly wedged writer still surfaces as a failed step.
_INDEX_RETRY_DELAYS_S = (0.0, 30.0, 90.0)
_INDEX_BUSY_TIMEOUT_MS = "60000"


def _index_with_retries(cfg: DaylogConfig) -> dict[str, object]:
    import os

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
            except Exception as exc:  # noqa: BLE001
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
    swallowed for a cycle (recheck finding 1).
    """
    if not dirty:
        return
    try:
        from smriti.daylog.lock import writer_lock

        with writer_lock(cfg.lock_path, timeout_s=30.0):
            state = DaylogState.load(cfg.state_path)
            for day in dirty:
                if day in digest_errors:
                    continue
                turns = read_day_turns(cfg.day_jsonl(day))
                if turns:
                    current = input_hash(turns)
                    if (read_input_hash(cfg.day_md(day)) != current
                            or read_input_hash(cfg.day_digest(day)) != current):
                        continue  # changed underneath us — stays dirty
                state.dirty_days.discard(day)
            state.save()
    except Exception:  # noqa: BLE001 — markers surviving too long is harmless
        log.warning("could not clear dirty-day markers", exc_info=True)


def _write_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{int(time.time())}")
    tmp.write_text(json.dumps(status, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)

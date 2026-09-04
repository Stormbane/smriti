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


def _stale_days(cfg: DaylogConfig, changed: set[date], target: date) -> list[str]:
    """Days needing render/digest: reconcile-changed (any age) + 7-day scan."""
    candidates = {day_key(d) for d in changed}
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
    try:
        from smriti.daylog.collect import collect_once

        state = DaylogState.load(cfg.state_path)
        report = collect_once(cfg, state)
        changed = report.changed_days
        steps["reconcile"] = {
            "ok": True,
            "appended": report.appended,
            "files_scanned": report.files_scanned,
            "parse_errors": report.parse_errors,
        }
    except Exception as exc:  # noqa: BLE001
        steps["reconcile"] = {"ok": False, "error": str(exc)[:300]}

    # 2+3+4. Repair/render/digest every stale day (reconcile-changed at any
    # age, plus the 7-day window).
    rendered: list[str] = []
    digested: list[str] = []
    try:
        for day in _stale_days(cfg, changed, target):
            if render_day(cfg, day) is not None:
                rendered.append(day)
            try:
                if digest_day(cfg, day, attempts) is not None:
                    digested.append(day)
            except Exception as exc:  # noqa: BLE001 — a failed digest leaves
                # the stale hash in place; the next nightly retries it.
                steps.setdefault("digest_errors", {})[day] = str(exc)[:300]  # type: ignore[union-attr]
        steps["render"] = {"ok": True, "days": rendered}
        steps["digest"] = {"ok": True, "days": digested}
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
        try:
            from smriti.store.indexer import index_tree

            stats = index_tree(root=cfg.root)
            steps["index"] = {"ok": True, **{k: int(v) for k, v in stats.items()}}
        except Exception as exc:  # noqa: BLE001
            steps["index"] = {"ok": False, "error": str(exc)[:300]}
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


def _write_status(path: Path, status: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{int(time.time())}")
    tmp.write_text(json.dumps(status, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)

"""``smriti logd`` — the day-log watcher daemon.

A polling tail over the configured source globs: one collection pass
every few seconds, forever. Recovery is structural, not heroic — the
pass is idempotent (dedup + persisted marks), so the supervisor can
kill and restart this process at any moment with no loss and no
duplicates; anything missed while down is backfilled by the next pass
or the nightly reconcile.

Every pass takes the writer lock only for its append burst, so the
nightly task interleaves freely with a running daemon.
"""

from __future__ import annotations

import logging
import time

from smriti.daylog.collect import collect_once
from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.state import DaylogState

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 5.0
_ERROR_BACKOFF_S = 30.0


def run_logd(
    cfg: DaylogConfig | None = None,
    *,
    interval_s: float = _DEFAULT_INTERVAL_S,
    max_passes: int | None = None,
) -> None:
    """Run the collection loop (``max_passes`` bounds it for tests)."""
    cfg = cfg or load_config()
    state = DaylogState.load(cfg.state_path)
    passes = 0
    log.info("logd: watching %d sources -> %s", len(cfg.sources), cfg.log_dir)
    while max_passes is None or passes < max_passes:
        passes += 1
        try:
            report = collect_once(cfg, state)
            if report.appended:
                log.info(
                    "logd: +%d turns (%d files scanned, %d parse errors)",
                    report.appended, report.files_scanned, report.parse_errors,
                )
            delay = interval_s
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("logd: pass failed; backing off")
            state = DaylogState.load(cfg.state_path)  # re-load, state may be torn
            delay = _ERROR_BACKOFF_S
        if max_passes is not None and passes >= max_passes:
            break
        time.sleep(delay)

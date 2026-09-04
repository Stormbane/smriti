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


def _pid_path(cfg: DaylogConfig):  # noqa: ANN202
    return cfg.log_dir / ".logd.pid"


def _running_pid(cfg: DaylogConfig) -> int:
    from smriti.daylog.lock import _pid_alive

    try:
        pid = int(_pid_path(cfg).read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        return 0
    return pid if _pid_alive(pid) else 0


def ensure_running(cfg: DaylogConfig | None = None) -> bool:
    """Keepalive entry point: start a detached logd if none is alive.

    Returns True if a daemon is (now) running. Called by the scheduled
    ``smriti logd --ensure`` task every 10 minutes — this is the
    self-recovery mechanism: a crashed daemon is restarted on the next
    tick, and the idempotent passes make the gap harmless.
    """
    import subprocess
    import sys

    cfg = cfg or load_config()
    if _running_pid(cfg):
        return True
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "smriti", "logd"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=flags,
    )
    log.info("logd: started daemon pid %d", proc.pid)
    return True


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
    try:
        _pid_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        _pid_path(cfg).write_text(str(__import__("os").getpid()), encoding="ascii")
    except OSError:
        pass  # keepalive degrades to always-spawn; dedup keeps that safe
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

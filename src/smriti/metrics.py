"""Unified metrics logging for smriti.

All smriti components write structured events to a single JSONL file at
``~/.narada/.smriti/metrics.jsonl``. This is the foundation for the eval
dashboard — every index, search, write, cascade, and sleep cycle produces
events that can be analyzed.

Usage::

    from smriti.metrics import get_logger

    metrics = get_logger()
    metrics.log("search_query", query="what is viveka", elapsed_ms=156, ...)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB before rotation
_MAX_ROTATIONS = 3


class MetricsLogger:
    """Append-only JSONL metrics logger with rotation."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = tree_root() / ".smriti" / "metrics.jsonl"
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: str, **data: Any) -> None:
        """Append a metric event to the log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        try:
            self._maybe_rotate()
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            log.warning("Metrics write failed: %s", exc)

    def read(
        self,
        *,
        since: str | None = None,
        event_type: str | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Read events from the log, optionally filtered.

        Parameters
        ----------
        since:
            ISO timestamp — only return events after this time.
        event_type:
            Filter to this event type (e.g. ``"search_query"``).
        limit:
            Maximum number of events to return (0 = all). Returns the
            most recent ``limit`` events.
        """
        if not self._path.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event_type and entry.get("event") != event_type:
                continue
            if since and entry.get("timestamp", "") < since:
                continue

            events.append(entry)

        if limit > 0:
            events = events[-limit:]

        return events

    def summary(self, since: str | None = None) -> dict[str, Any]:
        """Compute a summary of recent metrics."""
        events = self.read(since=since)
        if not events:
            return {"total_events": 0}

        by_type: dict[str, int] = {}
        total_tokens_in = 0
        total_tokens_out = 0
        total_cost = 0.0
        search_count = 0
        search_total_ms = 0
        index_count = 0
        write_count = 0
        cascade_verdicts: dict[str, int] = {}

        for e in events:
            evt = e.get("event", "unknown")
            by_type[evt] = by_type.get(evt, 0) + 1

            total_tokens_in += e.get("judge_tokens_in", 0) + e.get("executor_tokens_in", 0) + e.get("total_tokens_in", 0)
            total_tokens_out += e.get("judge_tokens_out", 0) + e.get("executor_tokens_out", 0) + e.get("total_tokens_out", 0)
            total_cost += e.get("total_cost_usd", 0.0) + e.get("cost_usd", 0.0)

            if evt == "search_query":
                search_count += 1
                search_total_ms += e.get("elapsed_ms", 0)
            elif evt == "index_completed":
                index_count += 1
            elif evt == "write_entry":
                write_count += 1
            elif evt == "cascade_verdict":
                v = e.get("verdict", "UNKNOWN")
                cascade_verdicts[v] = cascade_verdicts.get(v, 0) + 1

        return {
            "total_events": len(events),
            "events_by_type": by_type,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "total_cost_usd": round(total_cost, 4),
            "search_count": search_count,
            "avg_search_ms": round(search_total_ms / search_count, 1) if search_count else 0,
            "index_runs": index_count,
            "writes": write_count,
            "cascade_verdicts": cascade_verdicts,
            "period_start": events[0].get("timestamp", ""),
            "period_end": events[-1].get("timestamp", ""),
        }

    def _maybe_rotate(self) -> None:
        """Rotate log file if it exceeds the size limit."""
        if not self._path.exists():
            return
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < _MAX_FILE_SIZE:
            return

        # Rotate: metrics.jsonl → metrics.jsonl.1, .1 → .2, .2 → .3, .3 deleted
        for i in range(_MAX_ROTATIONS, 0, -1):
            old = self._path.with_suffix(f".jsonl.{i}")
            new = self._path.with_suffix(f".jsonl.{i + 1}") if i < _MAX_ROTATIONS else None
            if old.exists():
                if new:
                    old.rename(new)
                else:
                    old.unlink()

        self._path.rename(self._path.with_suffix(".jsonl.1"))
        log.info("Rotated metrics log (was %d bytes)", size)


# ── Singleton ────────────────────────────────────────────────────────

_instance: MetricsLogger | None = None


def get_logger(path: Path | None = None) -> MetricsLogger:
    """Return the singleton MetricsLogger."""
    global _instance
    if _instance is None or (path is not None and _instance.path != path):
        _instance = MetricsLogger(path)
    return _instance

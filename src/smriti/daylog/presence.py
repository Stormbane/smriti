"""Cross-channel presence: "Sam was on Telegram 3 minutes ago."

A deterministic tail-read of today's (and, near midnight, yesterday's)
day-log. Injected into live sessions via the recall hook so a Claude
Code session instantly knows a conversation is happening on another
channel. String formatting only; any failure is logged and returns ''
(never-break contract).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.model import Turn, day_key, day_tz
from smriti.daylog.writer import read_day_turns

log = logging.getLogger(__name__)

_WINDOW_MIN = 15
_PREVIEW_CHARS = 90


def _recent_turns(cfg: DaylogConfig, now: datetime) -> list[Turn]:
    local = now.astimezone(day_tz(cfg.root))
    days = {day_key(local.date())}
    days.add(day_key((local - timedelta(minutes=_WINDOW_MIN)).date()))
    turns: list[Turn] = []
    for day in days:
        turns.extend(read_day_turns(cfg.day_jsonl(day)))
    cutoff = now - timedelta(minutes=_WINDOW_MIN)
    return [t for t in turns if t.ts >= cutoff]


def presence_line(
    *,
    exclude_channel: str = "",
    now: datetime | None = None,
    cfg: DaylogConfig | None = None,
) -> str:
    """One line of other-channel activity in the last 15 minutes, or ''."""
    try:
        cfg = cfg or load_config()
        now = now or datetime.now(tz=day_tz(cfg.root))
        by_channel: dict[str, Turn] = {}
        for turn in _recent_turns(cfg, now):
            if exclude_channel and turn.channel == exclude_channel:
                continue
            latest = by_channel.get(turn.channel)
            if latest is None or turn.ts > latest.ts:
                by_channel[turn.channel] = turn
        if not by_channel:
            return ""
        parts: list[str] = []
        for channel, turn in sorted(by_channel.items(), key=lambda kv: kv[1].ts, reverse=True):
            age_min = max(0, int((now - turn.ts).total_seconds() // 60))
            who = cfg.speaker(turn.who)
            preview = turn.text.replace("\n", " ").strip()[:_PREVIEW_CHARS]
            parts.append(f'{channel} {age_min} min ago ({who}: "{preview}")')
        return "Active on other channels: " + "; ".join(parts[:3])
    except Exception as exc:  # noqa: BLE001 — presence must never break a session
        log.warning("no presence line: %s", exc)
        return ""

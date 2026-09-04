"""Turn model, stable identities, and event-time day placement.

The day boundary uses a fixed UTC offset (default +10:00, Australia/
Brisbane — no DST) rather than a tz database: the base install carries
no runtime dependencies and Windows has no system tzdata. Override with
``SMRITI_DAY_UTC_OFFSET`` (hours, may be fractional).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone


def day_tz() -> timezone:
    raw = os.environ.get("SMRITI_DAY_UTC_OFFSET", "10")
    try:
        hours = float(raw)
    except ValueError:
        hours = 10.0
    return timezone(timedelta(hours=hours))


def local_day(ts: datetime) -> date:
    """The local calendar day a timestamp belongs to."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(day_tz()).date()


def day_key(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_ts(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; returns aware UTC or None."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass
class Turn:
    """One conversational turn in the day-log.

    ``id`` is content-derived (channel + session + hash of ts/who/text)
    so re-reading a source range after a crash re-derives the same id
    and dedup absorbs the overlap.
    """

    ts: datetime
    channel: str
    who: str  # "suti" | "narada"
    text: str
    session: str = ""
    id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_turn_id(self.channel, self.session, self.ts, self.who, self.text)

    def day(self) -> date:
        return local_day(self.ts)

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.id,
            "ts": self.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "channel": self.channel,
            "who": self.who,
            "text": self.text,
            "session": self.session,
        }

    @classmethod
    def from_json(cls, obj: dict[str, object]) -> "Turn | None":
        ts = parse_ts(str(obj.get("ts", "")))
        text = obj.get("text")
        if ts is None or not isinstance(text, str):
            return None
        return cls(
            ts=ts,
            channel=str(obj.get("channel", "")),
            who=str(obj.get("who", "")),
            text=text,
            session=str(obj.get("session", "")),
            id=str(obj.get("id", "")),
        )


def make_turn_id(channel: str, session: str, ts: datetime, who: str, text: str) -> str:
    stamp = ts.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha1(f"{stamp}|{who}|{text}".encode("utf-8")).hexdigest()[:12]
    sess = (session or "-")[:8]
    return f"{channel}:{sess}:{digest}"

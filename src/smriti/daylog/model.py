"""Turn model, stable identities, and event-time day placement.

A turn is spoken by one of two roles, :data:`USER` or :data:`AGENT`. The
names shown for them come from config (``DaylogConfig.user_name`` and
``agent_name``), never from the data. Day-logs written before the roles
became generic name the reference installation's two speakers instead
(``"suti"``, ``"narada"``); those are read as the roles, forever.

A turn's day is the local day of its time, in the zone the day boundary
follows (:func:`day_tz`, from :mod:`smriti.core.dayzone`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

# The day zone is smriti.core.dayzone's, re-exported: the store's dated paths use it too.
from smriti.core.dayzone import SYSTEM_TIME as SYSTEM_TIME
from smriti.core.dayzone import ZONE_RECHECK_S as ZONE_RECHECK_S
from smriti.core.dayzone import day_tz as day_tz


USER = "user"
AGENT = "agent"
ROLES: tuple[str, str] = (USER, AGENT)

# ``who`` as day-logs stored it before the roles became generic.
_LEGACY_WHO: dict[str, str] = {"suti": USER, "narada": AGENT}

# What make_turn_id hashes for each role: the legacy tokens, not the role
# names. Every turn id minted before the roles became generic hashed
# "suti" or "narada", and ids must not change for the same content: a
# re-read or repair of an old source range would otherwise append every
# turn a second time, and every past day's input_hash would change, so the
# nightly would re-render and re-digest the whole history. Never edit this.
_ID_TOKEN: dict[str, str] = {USER: "suti", AGENT: "narada"}


def role_of(who: str) -> str:
    """The role a stored ``who`` names: a legacy name maps to its role; anything else is kept."""
    return _LEGACY_WHO.get(who, who)


def local_day(ts: datetime, root: Path | None = None) -> date:
    """The local calendar day a timestamp belongs to."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(day_tz(root)).date()


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

    ``who`` is a role, :data:`USER` or :data:`AGENT`. A legacy name
    (``"suti"``, ``"narada"``) is accepted and stored as its role.
    """

    ts: datetime
    channel: str
    who: str  # USER | AGENT
    text: str
    session: str = ""
    id: str = field(default="")

    def __post_init__(self) -> None:
        self.who = role_of(self.who)
        if not self.id:
            self.id = make_turn_id(self.channel, self.session, self.ts, self.who, self.text)

    def day(self, root: Path | None = None) -> date:
        return local_day(self.ts, root)

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
        """A stored record as a turn; a legacy ``who`` comes back as its role."""
        ts = parse_ts(str(obj.get("ts", "")))
        text = obj.get("text")
        if ts is None or not isinstance(text, str):
            return None
        return cls(
            ts=ts,
            channel=str(obj.get("channel", "")),
            who=role_of(str(obj.get("who", ""))),
            text=text,
            session=str(obj.get("session", "")),
            id=str(obj.get("id", "")),
        )


def make_turn_id(channel: str, session: str, ts: datetime, who: str, text: str) -> str:
    """``channel:session8:hash12``; the hash covers the time, the speaker and the text.

    The speaker is hashed as its legacy token (see ``_ID_TOKEN``), so a turn
    gets the same id whether *who* is a role or the legacy name.
    """
    stamp = ts.astimezone(timezone.utc).isoformat()
    role = role_of(who)
    token = _ID_TOKEN.get(role, role)
    digest = hashlib.sha1(f"{stamp}|{token}|{text}".encode("utf-8")).hexdigest()[:12]
    sess = (session or "-")[:8]
    return f"{channel}:{sess}:{digest}"

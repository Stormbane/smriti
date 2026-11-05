"""Turn model, stable identities, and event-time day placement.

A turn is spoken by one of two roles, :data:`USER` or :data:`AGENT`. The
names shown for them come from config (``DaylogConfig.user_name`` and
``agent_name``), never from the data. Day-logs written before the roles
became generic name the reference installation's two speakers instead
(``"suti"``, ``"narada"``); those are read as the roles, forever.

The day boundary follows the system's local time, daylight saving
included, unless ``SMRITI_DAY_UTC_OFFSET`` or daylog.json's ``timezone``
says otherwise (see :func:`day_tz`). It needs no tz database: the base
install carries no runtime dependencies and Windows has no system tzdata.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)

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


class _SystemTime(tzinfo):
    """The operating system's local time zone, daylight saving included.

    The OS converts each instant (``astimezone()`` with no zone), so this
    needs no tz database: Windows has none, and the base install carries no
    dependencies.
    """

    def fromutc(self, dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc).astimezone()

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._at(dt).utcoffset() or timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return self._at(dt).dst()

    def tzname(self, dt: datetime | None) -> str | None:
        return self._at(dt).tzname()

    @staticmethod
    def _at(dt: datetime | None) -> datetime:
        """The local time the OS gives for wall time ``dt`` (now, if None)."""
        if dt is None:
            return datetime.now().astimezone()
        try:
            return dt.replace(tzinfo=None).astimezone()
        except (OverflowError, OSError, ValueError):  # outside the OS's range
            return datetime.now().astimezone()

    def __repr__(self) -> str:
        return "SystemTime()"


SYSTEM_TIME = _SystemTime()


def day_tz(root: Path | None = None) -> tzinfo:
    """The zone the day boundary follows.

    In order: ``SMRITI_DAY_UTC_OFFSET`` (hours, may be fractional); then
    ``"timezone"`` in ``<root>/.smriti/daylog.json`` (``root`` defaults to the
    process's memory root; hours, ``"local"``, or
    an IANA name where Python can load one); then the system's local time.
    Every clock that names a day (day-log, digest, nightly, journal paths)
    asks here, so they agree.
    """
    raw = os.environ.get("SMRITI_DAY_UTC_OFFSET", "").strip()
    if raw:
        zone = _parse_zone(raw)
        if zone is not None:
            return zone
    configured = _configured_zone(root)
    if configured:
        zone = _parse_zone(configured)
        if zone is not None:
            return zone
    return SYSTEM_TIME


def _parse_zone(raw: str) -> tzinfo | None:
    if raw.lower() == "local":
        return SYSTEM_TIME
    try:
        return timezone(timedelta(hours=float(raw)))
    except ValueError:
        pass
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # No such zone, or no tz database here (Windows without tzdata); or a
        # key that isn't a relative path, or names a directory or a non-TZif file.
        log.warning("unknown time zone %r; using the system's local time", raw)
        return None


def _configured_zone(root: Path | None) -> str:
    from smriti.core.tree import tree_root

    path = (root if root is not None else tree_root()) / ".smriti" / "daylog.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return ""
    return _read_zone(str(path), stamp)


@functools.lru_cache(maxsize=8)
def _read_zone(path: str, stamp: int) -> str:
    """daylog.json's ``timezone``; cached per file version (day_tz runs per turn)."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("timezone", "")
    except (OSError, ValueError, AttributeError):
        return ""
    return str(value).strip()


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

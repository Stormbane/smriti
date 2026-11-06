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

import hashlib
import json
import logging
import os
import time
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
    asks here, so they agree. It runs once per captured turn, so daylog.json
    is looked at again at most once a second per root (see
    :func:`_configured_zone`): a change takes effect within a second.
    """
    global _env_zone
    raw = os.environ.get("SMRITI_DAY_UTC_OFFSET", "")
    if raw:
        seen, zone = _env_zone
        if raw != seen:  # parsed once per value, not once per turn
            zone = _parse_zone(raw.strip()) if raw.strip() else None
            _env_zone = (raw, zone)
        if zone is not None:
            return zone
    configured = _configured_zone(root)
    return configured if configured is not None else SYSTEM_TIME


_env_zone: tuple[str, tzinfo | None] = ("", None)  # SMRITI_DAY_UTC_OFFSET as last read, parsed


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


ZONE_RECHECK_S = 1.0  # how long a process trusts what it last read of daylog.json's zone
_clock = time.monotonic  # what that interval is measured with


@dataclass(frozen=True)
class _ZoneLook:
    """What a process last found in one root's daylog.json, and when it looked."""

    checked: float  # _clock() at the look
    path: str
    stamp: tuple[int, int] | None  # (st_mtime_ns, st_size); None when there is no file
    zone: tzinfo | None  # its "timezone", parsed; None when it names none (or an unknown one)


_zone_looks: dict[Path | None, _ZoneLook] = {}


def _configured_zone(root: Path | None) -> tzinfo | None:
    """daylog.json's ``timezone`` for ``root`` (the process's memory root when None).

    Within :data:`ZONE_RECHECK_S` of the last look this is a dict lookup:
    before the hardening the zone was an environment lookup only, and a
    ``stat`` per captured turn made a 10,000-turn reconcile's day lookups
    20 times slower. After it, the root is worked out again (it follows
    ``SMRITI_ROOT``, so a change of root is seen within the second too) and
    one ``stat`` says whether the file changed; it is read and parsed again
    only when it did.
    """
    now = _clock()
    look = _zone_looks.get(root)
    if look is not None and now - look.checked < ZONE_RECHECK_S:
        return look.zone
    from smriti.core.tree import tree_root

    path = str((root if root is not None else tree_root()) / ".smriti" / "daylog.json")
    try:
        st = os.stat(path)
        stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if look is not None and look.path == path and look.stamp == stamp:
        zone = look.zone
    else:
        raw = _read_zone(path) if stamp is not None else ""
        zone = _parse_zone(raw) if raw else None
    if len(_zone_looks) >= 16:
        _zone_looks.clear()  # roots come and go only in tests; never let it grow
    _zone_looks[root] = _ZoneLook(now, path, stamp, zone)
    return zone


def _read_zone(path: str) -> str:
    """daylog.json's ``timezone``, as written; "" when it has none or can't be read."""
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

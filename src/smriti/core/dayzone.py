"""The zone the day boundary follows: the system's local time, or a configured one.

The day boundary follows the system's local time, daylight saving
included, unless ``SMRITI_DAY_UTC_OFFSET`` or daylog.json's ``timezone``
says otherwise (see :func:`day_tz`). It needs no tz database: the base
install carries no runtime dependencies and Windows has no system tzdata.

Every clock that names a day asks here: the day-log, the digests and the
nightly (``smriti.daylog.model`` re-exports these names), and the store's
dated paths, so a journal entry and that moment's turns fall on the same day.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)


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

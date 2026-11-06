"""Which zone the day boundary follows, and that every day-naming clock agrees."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from smriti.core.tree import tree_root
from smriti.daylog import model
from smriti.daylog.model import SYSTEM_TIME, day_tz, local_day


@pytest.fixture
def no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMRITI_DAY_UTC_OFFSET", raising=False)


def _configure(zone: str) -> None:
    path = tree_root() / ".smriti" / "daylog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"timezone": zone, "location": "kept"}), encoding="utf-8")


def test_the_default_is_the_system_zone(no_override: None) -> None:
    """Outside review #3: the default was a fixed UTC+10 (the maintainer's)."""
    assert day_tz() is SYSTEM_TIME
    instant = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    assert instant.astimezone(day_tz()) == instant.astimezone()  # the OS's own conversion
    assert instant.astimezone(day_tz()).utcoffset() == instant.astimezone().utcoffset()


def test_daylight_saving_comes_from_the_os_per_instant(no_override: None) -> None:
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    for instant in (winter, summer):
        assert instant.astimezone(day_tz()).utcoffset() == instant.astimezone().utcoffset()


@pytest.mark.parametrize("zone,offset", [("5.5", 5.5), ("-4", -4.0), ("+0", 0.0)])
def test_daylog_json_sets_an_offset(no_override: None, zone: str, offset: float) -> None:
    _configure(zone)
    assert day_tz().utcoffset(None) == timedelta(hours=offset)


def test_local_in_config_is_the_system_zone(no_override: None) -> None:
    _configure("local")
    assert day_tz() is SYSTEM_TIME


def test_an_unknown_zone_falls_back_to_the_system(no_override: None,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    _configure("Mars/Olympus_Mons")
    assert day_tz() is SYSTEM_TIME
    assert "unknown time zone" in caplog.text


def test_the_environment_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure("-4")
    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
    assert day_tz().utcoffset(None) == timedelta(hours=10)


class Clock:
    """The monotonic clock day_tz measures its recheck interval with, moved by hand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(model, "_clock", lambda: self.now)


def test_a_config_change_is_seen_without_a_restart(
        no_override: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import time

    clock = Clock(monkeypatch)
    _configure("-4")
    assert day_tz().utcoffset(None) == timedelta(hours=-4)
    _configure("3")
    path = tree_root() / ".smriti" / "daylog.json"
    later = time.time() + 5
    os.utime(path, (later, later))  # a new mtime even on coarse clocks
    clock.now += 0.5
    assert day_tz().utcoffset(None) == timedelta(hours=-4)  # within the second: as last read
    clock.now += model.ZONE_RECHECK_S
    assert day_tz().utcoffset(None) == timedelta(hours=3)  # seen once the second is up


def test_ten_thousand_lookups_cost_a_handful_of_stats(
        no_override: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex regression audit: every captured turn ran a stat of daylog.json to
    check the cached zone, 0.59 s per 10,000 lookups against 0.03 s when the
    zone was an environment lookup only."""
    import os
    import time

    _configure("5.5")
    path = str(tree_root() / ".smriti" / "daylog.json")
    stats: list[str] = []
    real_stat = os.stat

    def counting_stat(p: object, *a: object, **k: object) -> os.stat_result:
        if os.fspath(p) == path:  # type: ignore[call-overload]
            stats.append(path)
        return real_stat(p, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", counting_stat)
    ts = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
    t0 = time.perf_counter()
    days = {local_day(ts) for _ in range(10_000)}
    took = time.perf_counter() - t0

    assert days == {datetime(2026, 11, 6).date()}  # 20:00 UTC is the next day at +5.5
    assert len(stats) <= 1 + int(took / model.ZONE_RECHECK_S) + 1  # one a second at most
    assert len(stats) <= 5


def test_a_changed_zone_is_seen_after_the_interval_and_only_then(
        no_override: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    clock = Clock(monkeypatch)
    _configure("2")
    path = tree_root() / ".smriti" / "daylog.json"
    looked: list[str] = []  # the stats of daylog.json (resolving the root stats too)
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (
        looked.append(os.fspath(p)) if os.fspath(p) == str(path) else None)
        or real_stat(p, *a, **k))
    assert day_tz().utcoffset(None) == timedelta(hours=2)
    assert looked == [str(path)]

    path.write_text(json.dumps({"timezone": "-7"}), encoding="utf-8")  # another size
    start = clock.now
    for i in range(100):
        clock.now = start + model.ZONE_RECHECK_S * i / 100
        assert day_tz().utcoffset(None) == timedelta(hours=2)
    assert len(looked) == 1  # nothing looked at the file within the second
    clock.now = start + model.ZONE_RECHECK_S
    assert day_tz().utcoffset(None) == timedelta(hours=-7)
    assert len(looked) == 2

    path.unlink()  # no config: the system's zone, within a second as well
    clock.now += model.ZONE_RECHECK_S
    assert day_tz() is SYSTEM_TIME


def test_an_explicit_root_uses_its_own_zone(tmp_path: object,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review: the zone was read from the process's default tree even when
    a caller named another root (write_entry(root=...), a DaylogConfig)."""
    from pathlib import Path

    from smriti.store import writer

    monkeypatch.delenv("SMRITI_DAY_UTC_OFFSET", raising=False)
    other = Path(str(tmp_path)) / "other-root"
    (other / ".smriti").mkdir(parents=True)
    (other / ".smriti" / "daylog.json").write_text('{"timezone": "-4"}', encoding="utf-8")
    _configure("10")  # the process's default tree says something else
    assert day_tz(other).utcoffset(None) == timedelta(hours=-4)
    assert day_tz().utcoffset(None) == timedelta(hours=10)

    fixed = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(writer, "datetime", FrozenClock)
    path = writer.write_entry("a note", branch="notes", root=other, reindex=False)
    assert path.name == "11-04.md"  # 02:00 UTC is the 4th at UTC-4, the 5th at UTC+10


def test_journal_entries_and_turns_name_the_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UTC-4 zone puts 02:00 UTC on the previous day; the journal must agree."""
    from smriti.store import writer

    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "-4")
    fixed = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(writer, "datetime", FrozenClock)
    path = writer.write_entry("a note", branch="notes", root=tree_root(), reindex=False)
    assert local_day(fixed).isoformat() == "2026-11-04"
    assert path.name == "11-04.md" and "date: 2026-11-04" in path.read_text(encoding="utf-8")
    assert model.day_key(local_day(fixed)) == "2026-11-04"

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


def test_a_config_change_is_seen_without_a_restart(no_override: None) -> None:
    import os
    import time

    _configure("-4")
    assert day_tz().utcoffset(None) == timedelta(hours=-4)
    _configure("3")
    path = tree_root() / ".smriti" / "daylog.json"
    later = time.time() + 5
    os.utime(path, (later, later))  # a new mtime even on coarse clocks
    assert day_tz().utcoffset(None) == timedelta(hours=3)


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

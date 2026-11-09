"""Project records are dated in the day zone, as the writer and the day-log date things (H15.7b).

Findings, decisions and feature specs named their files, and filled their
``date``/``opened``/``shipped`` fields, from the machine's local clock, while
a journal entry written at the same moment followed ``SMRITI_DAY_UTC_OFFSET``
or daylog.json's ``timezone``: the two could name different days.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pytest

from smriti.core.tree import tree_root
from smriti.mcp.tools.projects import (
    handle_record_decision,
    handle_record_feature,
    handle_record_finding,
    handle_update_feature_status,
)


def _zone_off_the_local_day(monkeypatch: pytest.MonkeyPatch) -> timezone:
    """A configured zone in which today is not the machine's local today.

    At any instant UTC+14 and UTC-12 are on different dates, so one of them
    differs from the local date.
    """
    local = datetime.now().astimezone().date()
    for hours in (14, -12):
        zone = timezone(timedelta(hours=hours))
        if datetime.now(zone).date() != local:
            monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", str(hours))
            return zone
    raise AssertionError("UTC+14 and UTC-12 on the same date")


def _days(zone: timezone, act: Callable[[], object]) -> set[str]:
    """The zone's date before and after ``act()`` (a midnight may fall between)."""
    before = datetime.now(zone).date()
    act()
    after = datetime.now(zone).date()
    return {d.isoformat() for d in (before, after)}


@pytest.fixture(autouse=True)
def mirror() -> None:
    (tree_root() / "mirrors" / "demo" / "ai").mkdir(parents=True)


def test_findings_and_decisions_are_dated_in_the_day_zone(
        monkeypatch: pytest.MonkeyPatch) -> None:
    zone = _zone_off_the_local_day(monkeypatch)
    local = datetime.now().astimezone().date().isoformat()

    days = _days(zone, lambda: (
        handle_record_finding({"project": "demo", "title": "Lamp oil", "finding": "f",
                               "why_it_matters": "w"}),
        handle_record_decision({"project": "demo", "title": "Wick length", "decision": "d",
                                "rationale": "r"}),
    ))
    assert local not in days
    root = tree_root() / "mirrors" / "demo"
    [finding] = (root / "findings").glob("*.md")
    [decision] = (root / "decisions").glob("*.md")
    for path in (finding, decision):
        day = path.name[:10]
        assert day in days, (path.name, days, local)
        assert f"\ndate: {day}\n" in path.read_text(encoding="utf-8")


def test_a_feature_is_opened_and_shipped_in_the_day_zone(
        monkeypatch: pytest.MonkeyPatch) -> None:
    zone = _zone_off_the_local_day(monkeypatch)
    local = datetime.now().astimezone().date().isoformat()

    days = _days(zone, lambda: (
        handle_record_feature({"project": "demo", "slug": "lamps", "title": "Lamps",
                               "goal": "Keep the lamps lit.", "plan": "Oil, wick, flame."}),
        handle_update_feature_status({"project": "demo", "slug": "lamps",
                                      "new_status": "shipped"}),
    ))
    assert local not in days
    spec = tree_root() / "mirrors" / "demo" / "ai" / "features" / "shipped" / "lamps.md"
    fields = dict(line.split(": ", 1) for line in spec.read_text(encoding="utf-8").splitlines()
                  if line.startswith(("opened: ", "shipped: ")))
    assert fields["opened"] in days and fields["shipped"] in days, (fields, days, local)
    assert date.fromisoformat(fields["shipped"])  # a date, not the template's blank

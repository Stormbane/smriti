"""The wake's SYSTEM line: one line from the doctor's last report
(host/doctor.json), so every session knows the home system's health
without asking. Silent with no report; an old report says it is old."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smriti.wake.briefing import SYSTEM_LINE_CAP, _system_line, briefing

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)  # 10:00 at UTC+10


def _report(root: Path, checks: list[dict], at: datetime = NOW - timedelta(minutes=10)) -> None:
    host = root / "host"
    host.mkdir(parents=True, exist_ok=True)
    (host / "doctor.json").write_text(json.dumps({
        "schema": "narada.doctor/1",
        "generated_at": at.isoformat().replace("+00:00", "Z"),
        "overall": "red",
        "checks": checks,
    }), encoding="utf-8")


def _c(cid: str, status: str, title: str | None = None) -> dict:
    return {"id": cid, "title": title or cid, "status": status, "reason": "x"}


def test_silent_without_a_report(tmp_path: Path) -> None:
    assert _system_line(tmp_path, NOW) == ""


def test_names_the_reds_and_counts_the_ambers(tmp_path: Path) -> None:
    _report(tmp_path, [_c("voice_box", "red", "BOX voice worker"), _c("box_device", "amber"),
                       _c("deadman", "green"), _c("log_hygiene", "amber")])
    line = _system_line(tmp_path, NOW)
    assert line == ("--- SYSTEM (doctor, 04 Nov 09:50): 1 red: BOX voice worker; 2 amber. "
                    "Details: the app (Data, System) or the doctor CLI ---\n")


def test_all_green_and_no_red(tmp_path: Path) -> None:
    _report(tmp_path, [_c("a", "green")])
    assert ": all green." in _system_line(tmp_path, NOW)
    _report(tmp_path, [_c("a", "green"), _c("b", "amber")])
    assert ": no red; 1 amber." in _system_line(tmp_path, NOW)


def test_an_old_report_says_it_is_old(tmp_path: Path) -> None:
    _report(tmp_path, [_c("a", "green")], at=NOW - timedelta(days=2))
    assert "SYSTEM (doctor, STALE: last ran 02 Nov 10:00)" in _system_line(tmp_path, NOW)


def test_a_broken_report_never_breaks_the_wake(tmp_path: Path) -> None:
    (tmp_path / "host").mkdir()
    (tmp_path / "host" / "doctor.json").write_text("{not json", encoding="utf-8")
    assert _system_line(tmp_path, NOW) == ""
    (tmp_path / "host" / "doctor.json").write_text(json.dumps({"checks": []}), encoding="utf-8")
    assert _system_line(tmp_path, NOW) == ""


def test_many_reds_stay_within_the_cap(tmp_path: Path) -> None:
    _report(tmp_path, [_c(f"check_{i}", "red", f"A long check title number {i}") for i in range(30)])
    line = _system_line(tmp_path, NOW)
    assert len(line) <= SYSTEM_LINE_CAP + 1 and line.endswith("… ---\n")


def test_the_briefing_carries_it(tmp_path: Path) -> None:
    _report(tmp_path, [_c("voice_box", "red", "BOX voice worker")])
    text = briefing(tmp_path, cwd=tmp_path, now=NOW)
    assert "--- SYSTEM (doctor, 04 Nov 09:50): 1 red: BOX voice worker." in text

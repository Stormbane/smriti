"""The nightly's stale-day scan reads only the day-files that changed (hardening H13.8).

Every candidate day's render and digest heads are checked every night; what
the scan stops doing is reading every day-file in the history to hash its
turns. A day-file outside the signalled days (reconcile-changed, dirty, the
7-day window) is read when its size or mtime differs from the marker's, and
every file is read when the marker is missing, unreadable or a week past its
last full scan. The result is the full scan's: the same stale days, corrupt
lines, digests and rollups.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog import nightly
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, day_key, parse_ts
from smriti.daylog.writer import append_turns
from smriti.llm.types import LLMRequest, LLMResponse

TARGET = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc)  # 03:05 on 09-05 at UTC+10
OLD = ["2026-07-01", "2026-07-02", "2026-07-03"]  # outside the window, never signalled
RECENT = "2026-09-03"
WINDOW = sorted(day_key(TARGET - timedelta(days=back)) for back in range(7))


class FakeProvider:
    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="## Highlights\n- a digest")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: FakeProvider())


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The days whose day-file the scan reads, in order."""
    seen: list[str] = []
    real = nightly.read_day

    def counting(path: Path) -> object:
        seen.append(f"{path.parent.parent.name}-{path.parent.name}-{path.stem}")
        return real(path)

    monkeypatch.setattr(nightly, "read_day", counting)
    return seen


def _cfg(root: Path, agent: str = "Ari") -> DaylogConfig:
    return DaylogConfig(root=root, sources=[], user_name="Sam", agent_name=agent)


def _turn(ts: str, text: str, who: str = USER) -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel="voice", who=who, text=text, session="s1")


def _seed(cfg: DaylogConfig, day: str, text: str = "") -> None:
    append_turns(cfg, [_turn(f"{day}T01:00:00Z", text or f"Sam on {day}"),
                       _turn(f"{day}T01:00:05Z", f"Ari on {day}", who=AGENT)])


def _tree(root: Path) -> DaylogConfig:
    """Old days and a recent one, every render and digest current, and the marker written."""
    cfg = _cfg(root)
    for day in [*OLD, RECENT]:
        _seed(cfg, day)
    nightly.run_nightly(cfg, now=NOW, reindex=False, witness=False)
    assert nightly._scan_days(cfg, set(), set(), TARGET).stale == []
    return cfg


def _scan(cfg: DaylogConfig, target: date = TARGET) -> nightly._Scan:
    return nightly._scan_days(cfg, set(), set(), target)


def _marker(cfg: DaylogConfig) -> Path:
    return cfg.root / ".smriti" / "nightly-scan.json"


def _full(cfg: DaylogConfig, target: date = TARGET) -> nightly._Scan:
    """The full scan's answer: the same scan with no marker (the marker is put back)."""
    path = _marker(cfg)
    saved = path.read_bytes()
    path.unlink()
    try:
        scan = _scan(cfg, target)
    finally:
        path.write_bytes(saved)
    assert scan.full
    return scan


def test_a_changed_day_is_read_and_an_unchanged_one_is_not(
        tmp_path: Path, reads: list[str]) -> None:
    cfg = _tree(tmp_path)
    reads.clear()
    scan = _scan(cfg)
    assert reads == WINDOW  # the signalled days only; none of the old ones
    assert (scan.stale, scan.read, scan.days, scan.full) == ([], 7, 4, False)

    _seed(cfg, OLD[1], "Sam, a month late")  # a change the nightly wasn't told about
    reads.clear()
    scan = _scan(cfg)
    assert reads == [OLD[1], *WINDOW]
    assert scan.stale == [OLD[1]]
    reads.clear()
    full = _full(cfg)
    assert sorted(reads) == sorted([*OLD, *WINDOW])
    assert (full.stale, full.corrupt) == (scan.stale, scan.corrupt)


def test_derived_files_are_checked_every_night_without_reading_the_log(
        tmp_path: Path, reads: list[str]) -> None:
    """The heads are read every night, so a render that went, or a new digest prompt,
    makes an old day stale though its day-file is unchanged and isn't read."""
    cfg = _tree(tmp_path)
    cfg.day_md(OLD[0]).unlink()
    reads.clear()
    assert _scan(cfg).stale == [OLD[0]]
    assert OLD[0] not in reads

    renamed = _cfg(tmp_path, agent="Ari Two")  # the agent's name is in the digest's fingerprint
    reads.clear()
    scan = nightly._scan_days(renamed, set(), set(), TARGET)
    assert scan.stale == [*OLD, RECENT]
    assert reads == WINDOW
    assert scan.stale == _full(renamed).stale


def test_a_days_corrupt_lines_are_reported_without_reading_it_again(
        tmp_path: Path, reads: list[str]) -> None:
    cfg = _tree(tmp_path)
    with cfg.day_jsonl(OLD[2]).open("a", encoding="utf-8", newline="\n") as f:
        f.write("not a turn\n")
    first = _scan(cfg)
    reads.clear()
    second = _scan(cfg)
    assert first.corrupt == second.corrupt == {OLD[2]: 1}
    assert OLD[2] not in reads
    assert second.corrupt == _full(cfg).corrupt


def _damage_missing(path: Path) -> None:
    path.unlink()


def _damage_text(path: Path) -> None:
    path.write_text("{not json", encoding="utf-8")


def _damage_version(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**raw, "version": 99}), encoding="utf-8")


def _damage_entry(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["days"][OLD[0]] = ["big", 1, "h", 0]
    path.write_text(json.dumps(raw), encoding="utf-8")


def _damage_shape(path: Path) -> None:
    path.write_text(json.dumps(["a", "list"]), encoding="utf-8")


@pytest.mark.parametrize("damage", [_damage_missing, _damage_text, _damage_version,
                                    _damage_entry, _damage_shape])
def test_a_missing_or_corrupt_marker_means_a_full_scan(
        tmp_path: Path, reads: list[str], damage: object) -> None:
    cfg = _tree(tmp_path)
    _seed(cfg, OLD[0], "Sam again")
    expected = _full(cfg)
    damage(_marker(cfg))  # type: ignore[operator]
    reads.clear()
    scan = _scan(cfg)
    assert scan.full
    assert sorted(reads) == sorted([*OLD, *WINDOW])
    assert (scan.stale, scan.corrupt) == (expected.stale, expected.corrupt) == ([OLD[0]], {})
    reads.clear()
    assert not _scan(cfg).full  # the marker is whole again
    assert reads == WINDOW


def test_a_full_scan_runs_weekly_and_finds_what_a_size_and_mtime_hide(
        tmp_path: Path, reads: list[str]) -> None:
    cfg = _tree(tmp_path)
    # A rewrite that keeps the size and the mtime: one turn's id changes, so its hash does.
    path = cfg.day_jsonl(OLD[0])
    st = path.stat()
    text = path.read_text(encoding="utf-8")
    record = json.loads(text.splitlines()[0])
    swapped = record["id"][:-1] + ("0" if record["id"][-1] != "0" else "1")
    path.write_text(text.replace(record["id"], swapped), encoding="utf-8", newline="")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size

    six_days_on = TARGET + timedelta(days=6)
    assert not _scan(cfg, six_days_on).full
    assert OLD[0] not in _scan(cfg, six_days_on).stale  # hidden from the incremental scan
    reads.clear()
    week_on = _scan(cfg, TARGET + timedelta(days=7))
    assert week_on.full and OLD[0] in reads
    assert OLD[0] in week_on.stale
    assert not _scan(cfg, TARGET + timedelta(days=8)).full  # the week starts again
    assert _scan(cfg, TARGET).full  # a target before the last full scan: the clock went back


def test_a_day_file_that_could_not_be_read_is_read_again_the_next_night(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reads: list[str]) -> None:
    """A failed read isn't recorded: its size and mtime would stand for turns never read,
    and the day would wait for the weekly full scan. The night says which it couldn't read."""
    cfg = _tree(tmp_path)
    _seed(cfg, OLD[1], "Sam, a month late")  # changed, and nothing signals it
    path = cfg.day_jsonl(OLD[1])
    real_open = Path.open

    def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == path:
            raise PermissionError(13, "held by a scanner", str(self))
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(Path, "open", flaky_open)
        failed = _scan(cfg)
    assert (failed.stale, failed.unreadable) == ([], [OLD[1]])
    assert OLD[1] not in json.loads(_marker(cfg).read_text(encoding="utf-8"))["days"]

    reads.clear()
    recovered = _scan(cfg)  # access is back; the size and mtime haven't changed
    assert OLD[1] in reads
    assert (recovered.stale, recovered.unreadable) == ([OLD[1]], [])


def test_read_day_says_whether_it_read_the_whole_file(tmp_path: Path) -> None:
    from smriti.daylog.writer import read_day

    cfg = _cfg(tmp_path)
    _seed(cfg, OLD[0])
    whole = read_day(cfg.day_jsonl(OLD[0]))
    assert whole.complete and len(whole.turns) == 2
    missing = read_day(tmp_path / "log" / "2026" / "01" / "01.jsonl")
    assert missing.complete and missing.turns == []  # no file: no turns, a whole answer
    unreadable = read_day(tmp_path)  # a directory: an OSError other than not-found
    assert not unreadable.complete and unreadable.turns == []


def test_the_status_names_a_day_file_the_night_could_not_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.daylog.writer import DayTurns

    cfg = _tree(tmp_path)
    real = nightly.read_day
    monkeypatch.setattr(nightly, "read_day", lambda p: DayTurns(complete=False)
                        if p == cfg.day_jsonl(RECENT) else real(p))
    status = nightly.run_nightly(cfg, now=NOW, reindex=False, witness=False)
    render = status["steps"]["render"]  # type: ignore[index]
    assert render["unreadable"] == [RECENT]  # type: ignore[index]
    assert status["ok"] is True


def test_a_marker_that_cannot_be_saved_leaves_the_scan_whole(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _tree(tmp_path)
    _marker(cfg).unlink()

    def refuse(path: Path, text: str | bytes, **kw: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(nightly, "write_atomic", refuse)
    with caplog.at_level(logging.WARNING, logger="smriti.daylog.nightly"):
        scan = _scan(cfg)
    assert scan.full and scan.stale == []
    assert "scan marker" in caplog.text
    assert not _marker(cfg).exists()


def _nights(cfg: DaylogConfig, *, always_full: bool) -> list[dict[str, object]]:
    """Three nights a day apart; a late turn lands on an old day before the second."""
    out = []
    for n in range(3):
        if n == 1:
            append_turns(cfg, [_turn(f"{OLD[1]}T05:00:00Z", "a late turn")])
        if always_full:
            _marker(cfg).unlink(missing_ok=True)
        status = nightly.run_nightly(cfg, now=NOW + timedelta(days=n), reindex=False,
                                     witness=False)
        steps = status["steps"]
        assert isinstance(steps, dict)
        out.append({"render": steps["render"]["days"], "digest": steps["digest"]["days"],
                    "rollups": steps["rollups"]["built"], "scan": steps["render"]["scan"]})
    return out


def test_nights_schedule_what_full_scans_schedule(tmp_path: Path) -> None:
    def seeded(name: str) -> DaylogConfig:
        cfg = _cfg(tmp_path / name)
        for day in [*OLD, "2026-08-10", "2026-08-11", RECENT]:
            _seed(cfg, day)
        return cfg

    incremental = _nights(seeded("incremental"), always_full=False)
    full = _nights(seeded("full"), always_full=True)
    strip = [{k: v for k, v in night.items() if k != "scan"} for night in incremental]
    assert strip == [{k: v for k, v in night.items() if k != "scan"} for night in full]
    assert OLD[1] in incremental[1]["digest"]  # type: ignore[operator]
    assert [night["scan"] for night in incremental] == [
        {"days": 6, "read": 12, "full": True},  # six day-files and the window's other six days
        {"days": 6, "read": 8, "full": False},  # the window and the late day
        {"days": 6, "read": 7, "full": False},
    ]
    marker = json.loads((tmp_path / "incremental" / ".smriti" / "nightly-scan.json")
                        .read_text(encoding="utf-8"))
    assert marker["version"] == 1 and marker["full_scan"] == "2026-09-04"
    assert sorted(marker["days"]) == [*OLD, "2026-08-10", "2026-08-11", RECENT]

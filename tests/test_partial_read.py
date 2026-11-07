"""A day-file read only partway is never published (hardening H14.3).

``read_day`` says when it couldn't read a day-file to its end (``complete``
False, the turns before the failure), but the render took the prefix as the
whole day and wrote it, and the digest summarised that (Codex, outside review
#7). Now what publishes (the render, the digest) reads with
``read_whole_day``, which raises ``DayUnreadable`` instead; the nightly keeps
the previous render and digest, names the day in its status (and the wake
line), keeps its dirty marker, and reads it again the next night.

A partial read is made the way a failing disk or a scanner's lock makes one:
the file gives its first line, then the read raises OSError.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog import nightly
from smriti.daylog.state import DaylogState
from smriti.daylog.writer import (
    DayUnreadable,
    append_turns,
    read_day,
    read_day_turns,
    read_whole_day,
)
from smriti.wake.briefing import _nightly_line
from tests.test_nightly_scan import NOW, RECENT, FakeProvider, _cfg, _seed, _tree, _turn


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: FakeProvider())


class _Torn:
    """A day-file that gives its first line, then fails, as a disk error partway would."""

    def __init__(self, f: IO[str]) -> None:
        self._f = f

    def __enter__(self) -> _Torn:
        return self

    def __exit__(self, *exc: object) -> None:
        self._f.close()

    def __iter__(self) -> Iterator[str]:
        yield self._f.readline()
        raise OSError(5, "Input/output error")


def _tear(monkeypatch: pytest.MonkeyPatch, path: Path, *, after: int = 0) -> None:
    """Every open of *path* to read it, past the first *after*, stops after one line."""
    real_open = Path.open
    opened = [0]

    def torn_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        f = real_open(self, mode, *args, **kwargs)  # type: ignore[call-overload]
        if self == path and mode == "r":
            opened[0] += 1
            if opened[0] > after:
                return _Torn(f)
        return f

    monkeypatch.setattr(Path, "open", torn_open)


def test_read_whole_day_refuses_a_read_that_stops_partway(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, RECENT)
    path = cfg.day_jsonl(RECENT)
    assert len(read_whole_day(path)) == 2
    _tear(monkeypatch, path)
    partial = read_day(path)
    assert not partial.complete and len(partial.turns) == 1
    with pytest.raises(DayUnreadable):
        read_whole_day(path)
    assert len(read_day_turns(path)) == 1  # the prefix, for a caller that only looks
    assert read_whole_day(tmp_path / "log" / "2026" / "01" / "01.jsonl") == []  # missing: whole


def test_render_and_digest_refuse_a_partial_read_and_leave_their_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.daylog.render import render_day, render_snapshot

    cfg = _tree(tmp_path)
    md, digest = cfg.day_md(RECENT), cfg.day_digest(RECENT)
    before = md.read_bytes(), digest.read_bytes()
    append_turns(cfg, [_turn(f"{RECENT}T02:00:00Z", "Sam, one more thing")])
    _tear(monkeypatch, cfg.day_jsonl(RECENT))
    with pytest.raises(DayUnreadable):
        render_day(cfg, RECENT)
    with pytest.raises(DayUnreadable):
        render_snapshot(cfg, RECENT)
    with pytest.raises(DayUnreadable):
        digest_mod.digest_day(cfg, RECENT, [])  # without a snapshot it reads the day itself
    assert (md.read_bytes(), digest.read_bytes()) == before


@pytest.mark.parametrize("who", ["scan", "render"])
def test_a_night_that_reads_a_day_partway_leaves_its_render_and_digest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, who: str) -> None:
    """``scan``: the stale-day scan's read stops partway, so the day isn't judged.
    ``render``: the scan reads it whole and finds it stale, then the render's read stops
    partway. Either way the night publishes nothing from it, says so, and the next
    night, reading it whole, brings both up to date."""
    cfg = _tree(tmp_path)
    md, digest = cfg.day_md(RECENT), cfg.day_digest(RECENT)
    before = md.read_bytes(), digest.read_bytes()
    append_turns(cfg, [_turn(f"{RECENT}T02:00:00Z", "Sam, one more thing")])
    state = DaylogState.load(cfg.state_path)
    state.dirty_days.add(RECENT)
    state.save()

    with monkeypatch.context() as m:
        _tear(m, cfg.day_jsonl(RECENT), after=0 if who == "scan" else 1)
        status = nightly.run_nightly(cfg, now=NOW, reindex=False, witness=False)
    steps = status["steps"]
    assert isinstance(steps, dict)
    assert steps["render"]["unreadable"] == [RECENT]
    assert RECENT not in steps["render"]["days"] and RECENT not in steps["digest"]["days"]
    assert (md.read_bytes(), digest.read_bytes()) == before
    assert RECENT in DaylogState.load(cfg.state_path).dirty_days  # still to repair
    assert status["ok"] is True  # nothing failed that the next night doesn't retry
    assert "could not read the day-log of 2026-09-03, read again next night" in (
        _nightly_line(cfg.root))

    status = nightly.run_nightly(cfg, now=NOW, reindex=False, witness=False)
    steps = status["steps"]
    assert isinstance(steps, dict)
    assert "unreadable" not in steps["render"] and steps["digest"]["days"] == [RECENT]
    assert "Sam, one more thing" in md.read_text(encoding="utf-8")
    assert digest.read_bytes() != before[1]
    assert RECENT not in DaylogState.load(cfg.state_path).dirty_days
    assert "could not read" not in _nightly_line(cfg.root)

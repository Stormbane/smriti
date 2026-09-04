"""Nightly-cycle tests: render, digest, rollups, orchestration, morning."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, parse_ts
from smriti.daylog.morning import run_morning
from smriti.daylog.nightly import run_nightly
from smriti.daylog.render import input_hash, read_input_hash, render_day
from smriti.daylog.rollup import enumerate_stale_periods, week_of
from smriti.daylog.writer import append_turns, read_day_turns
from smriti.llm.types import LLMRequest, LLMResponse


class FakeProvider:
    """Deterministic LLM stub; records the requests it served."""

    def __init__(self, text: str = "## Highlights\n- test digest") -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: provider)
    return provider


def _cfg(tmp_path: Path) -> DaylogConfig:
    return DaylogConfig(root=tmp_path, sources=[])


def _turn(ts: str, text: str, who: str = "suti", channel: str = "voice",
          session: str = "s1") -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel=channel, who=who, text=text, session=session)


def _seed_day(cfg: DaylogConfig, day_utc: str = "2026-09-04") -> None:
    append_turns(cfg, [
        _turn(f"{day_utc}T01:00:00Z", "morning question"),
        _turn(f"{day_utc}T01:00:05Z", "morning answer", who="narada"),
        _turn(f"{day_utc}T02:00:00Z", "loop iteration output", who="narada",
              channel="claude:smriti", session="auto1"),
    ])


# ---------------------------------------------------------------- render


def test_render_writes_timeline_with_hash_and_autonomous_marker(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    out = render_day(cfg, "2026-09-04")
    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert "**Suti:** morning question" in text
    assert "**Narada:** morning answer" in text
    # The zero-human session is a derived marker, not inline turns.
    assert "[autonomous: claude:smriti" in text
    assert "loop iteration output" not in text
    turns = read_day_turns(cfg.day_jsonl("2026-09-04"))
    assert read_input_hash(out) == input_hash(turns)


def test_render_marker_retracts_when_human_turn_arrives(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    render_day(cfg, "2026-09-04")
    append_turns(cfg, [_turn("2026-09-04T02:30:00Z", "actually hi", channel="claude:smriti",
                             session="auto1")])
    text = render_day(cfg, "2026-09-04").read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "[autonomous" not in text
    assert "loop iteration output" in text


# ---------------------------------------------------------------- digest


def test_digest_writes_file_with_input_hash(tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    render_day(cfg, "2026-09-04")
    attempts: list[dict[str, str]] = []
    out = digest_mod.digest_day(cfg, "2026-09-04", attempts)
    assert out is not None and out.name == "04-digest.md"
    assert "test digest" in out.read_text(encoding="utf-8")
    assert attempts == [{"provider": "claude_cli", "outcome": "ok"}]
    # The compose subprocess is constrained to no tools.
    assert fake_llm.requests[0].cli_args == ["--strict-mcp-config", "--disallowedTools", "*"]


def test_digest_falls_back_when_primary_fails(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    render_day(cfg, "2026-09-04")
    good = FakeProvider("fallback digest")

    class Boom:
        def is_available(self) -> bool:
            return True

        def call(self, request: LLMRequest) -> LLMResponse:
            raise RuntimeError("primary down")

    providers = {"claude_cli": Boom(), "codex_cli": good}
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: providers[name])
    attempts: list[dict[str, str]] = []
    out = digest_mod.digest_day(cfg, "2026-09-04", attempts)
    assert out is not None and "fallback digest" in out.read_text(encoding="utf-8")
    assert [a["provider"] for a in attempts] == ["claude_cli", "codex_cli"]
    assert attempts[0]["outcome"].startswith("error")


# ---------------------------------------------------------------- rollups


def test_week_of_convention() -> None:
    assert week_of(1) == 1 and week_of(7) == 1
    assert week_of(8) == 2 and week_of(28) == 4
    assert week_of(29) == 5 and week_of(31) == 5


def test_rollup_scan_finds_closed_weeks_and_repairs_stale(
    tmp_path: Path, fake_llm: FakeProvider
) -> None:
    cfg = _cfg(tmp_path)
    for day in ("2026-09-01", "2026-09-02", "2026-09-05"):
        _seed_day(cfg, day)
        render_day(cfg, day)
        digest_mod.digest_day(cfg, day, [])
    today = datetime(2026, 9, 20, tzinfo=timezone.utc).date()
    stale = enumerate_stale_periods(cfg, today)
    # Week1 (days 1-7) is closed and stale; week3 (day 20 not past) not closed.
    assert [p.kind for p in stale] == ["week"]
    from smriti.daylog.rollup import build_rollup

    out = build_rollup(stale[0], [])
    assert out.name == "week1.md"
    # Now up to date; a repaired member digest makes it stale again.
    assert enumerate_stale_periods(cfg, today) == []
    append_turns(cfg, [_turn("2026-09-01T03:00:00Z", "late arrival")])
    render_day(cfg, "2026-09-01")
    digest_mod.digest_day(cfg, "2026-09-01", [])
    assert [p.out_path.name for p in enumerate_stale_periods(cfg, today)] == ["week1.md"]


# ---------------------------------------------------------------- nightly


def test_nightly_renders_digests_and_writes_status(
    tmp_path: Path, fake_llm: FakeProvider
) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)  # UTC 2026-09-04 => Brisbane 2026-09-04 (early-morning turns)
    # 17:05 UTC = 03:05 Brisbane on 09-05 => target day is the just-closed 09-04.
    status = run_nightly(cfg, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc), reindex=False)
    assert status["target_day"] == "2026-09-04"
    assert cfg.day_md("2026-09-04").exists()
    assert cfg.day_digest("2026-09-04").exists()
    saved = json.loads(cfg.nightly_status_path.read_text(encoding="utf-8"))
    assert saved["steps"]["digest"]["days"] == ["2026-09-04"]
    assert saved["ok"] is True
    # Second run is a no-op (idempotent by input hash).
    status2 = run_nightly(cfg, now=datetime(2026, 9, 4, 17, 10, tzinfo=timezone.utc),
                          reindex=False)
    assert status2["steps"]["digest"]["days"] == []  # type: ignore[index]


def test_nightly_survives_digest_failure_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)

    class Boom:
        def is_available(self) -> bool:
            return True

        def call(self, request: LLMRequest) -> LLMResponse:
            raise RuntimeError("seat down")

    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: Boom())
    status = run_nightly(cfg, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc),
                         reindex=False)
    steps = status["steps"]
    # A failed digest is a failed night: step not ok, whole status not ok.
    assert steps["digest"]["ok"] is False  # type: ignore[index]
    assert "2026-09-04" in steps["digest"]["errors"]  # type: ignore[index,operator]
    assert status["ok"] is False
    assert cfg.day_md("2026-09-04").exists()  # render still happened
    # The stale digest hash means the next night retries.
    assert read_input_hash(cfg.day_digest("2026-09-04")) == ""


def test_nightly_repairs_daemon_marked_dirty_day_beyond_window(
    tmp_path: Path, fake_llm: FakeProvider
) -> None:
    """A historical day the daemon captured (dirty marker) is repaired even
    though reconcile sees nothing new and the day is outside 7 days."""
    from smriti.daylog.state import DaylogState

    cfg = _cfg(tmp_path)
    _seed_day(cfg, "2026-07-01")  # far outside the window
    state = DaylogState.load(cfg.state_path)
    state.mark_dirty({"2026-07-01"})
    state.save()
    status = run_nightly(cfg, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc),
                         reindex=False)
    assert "2026-07-01" in status["steps"]["digest"]["days"]  # type: ignore[index]
    assert DaylogState.load(cfg.state_path).dirty_days == set()


# ---------------------------------------------------------------- morning


def _notify_to_file(tmp_path: Path) -> tuple[list[str], Path]:
    out = tmp_path / "sent.txt"
    script = f"import sys,pathlib; pathlib.Path(r'{out}').write_text(sys.stdin.read())"
    return [sys.executable, "-c", script], out


def test_morning_sends_once_and_only_once(tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    cfg.notify_cmd, sent = _notify_to_file(tmp_path)
    now = datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)  # 07:00 Brisbane 09-05
    result = run_morning(cfg, now=now)
    assert result["sent"] is True
    assert sent.read_text(encoding="utf-8") == fake_llm.text
    # Same day again: ledger blocks a second send.
    again = run_morning(cfg, now=now)
    assert again["sent"] is False and "ledger" in str(again["reason"])


def test_morning_fail_closed_when_delivery_fails(tmp_path: Path,
                                                 fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    cfg.notify_cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    now = datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)
    result = run_morning(cfg, now=now)
    assert result["sent"] is False
    # Ledger was written BEFORE the send attempt: no retry, no double send.
    assert run_morning(cfg, now=now)["reason"].startswith("ledger")  # type: ignore[union-attr]


def test_morning_requires_notify_cmd(tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    result = run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))
    assert result["sent"] is False and "notify_cmd" in str(result["reason"])


# --------------------------------------------------------------- presence


def test_presence_line_reports_other_channels_only(tmp_path: Path) -> None:
    from smriti.daylog.presence import presence_line

    cfg = _cfg(tmp_path)
    now = parse_ts("2026-09-04T11:20:00Z")
    assert now is not None
    append_turns(cfg, [
        _turn("2026-09-04T11:16:22Z", "Is the voice still working?", channel="telegram"),
        _turn("2026-09-04T09:00:00Z", "old turn", channel="voice"),  # outside window
        _turn("2026-09-04T11:18:00Z", "own channel", channel="claude:smriti"),
    ])
    line = presence_line(exclude_channel="claude:smriti", now=now, cfg=cfg)
    assert "telegram" in line and "3 min ago" in line
    assert "old turn" not in line and "own channel" not in line


def test_presence_line_empty_on_missing_log(tmp_path: Path) -> None:
    from smriti.daylog.presence import presence_line

    assert presence_line(cfg=_cfg(tmp_path)) == ""


# -------------------------------------------------------------------- CLI


def test_cli_sleep_refuses_without_deep(capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    assert main(["sleep"]) == 1
    out = capsys.readouterr().out
    assert "smriti nightly" in out and "--deep" in out


# ---------------------------------------------------------------- briefing


def test_wake_briefing_reports_while_you_slept(tmp_path: Path) -> None:
    from smriti.wake.briefing import briefing

    status = {"target_day": "2026-09-04", "ok": True,
              "steps": {"digest": {"ok": True, "days": ["2026-09-04"]}}}
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / ".nightly-status.json").write_text(json.dumps(status), encoding="utf-8")
    text = briefing(tmp_path, cwd=tmp_path)
    assert "WHILE YOU SLEPT: nightly ok for 2026-09-04" in text
    assert "2026-09-04" in text


def test_wake_briefing_surfaces_nightly_failure(tmp_path: Path) -> None:
    from smriti.wake.briefing import briefing

    status = {"target_day": "2026-09-04", "ok": False,
              "steps": {"reconcile": {"ok": False, "error": "boom"},
                        "digest": {"ok": True, "days": []}}}
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / ".nightly-status.json").write_text(json.dumps(status), encoding="utf-8")
    text = briefing(tmp_path, cwd=tmp_path)
    assert "NIGHTLY HAD FAILURES for 2026-09-04: reconcile" in text


def test_wake_briefing_silent_without_nightly_status(tmp_path: Path) -> None:
    from smriti.wake.briefing import briefing

    assert "WHILE YOU SLEPT" not in briefing(tmp_path, cwd=tmp_path)

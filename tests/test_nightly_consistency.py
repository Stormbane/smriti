"""H12.11: one consistent nightly.

One run at a time per memory root (a manual ``smriti nightly`` and the 03:00
task overlapped before), writes through unique temp names, and a digest
that hashes exactly the turns it summarised.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.core.oslock import UNAVAILABLE, exclusive_lock
from smriti.daylog import digest as digest_mod
from smriti.daylog import nightly
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
from smriti.daylog.nightly import run_lock_key, run_nightly
from smriti.daylog.writer import append_turns
from smriti.llm.types import LLMRequest, LLMResponse

NIGHT_1 = datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc)  # 03:05 on 09-05, UTC+10
NIGHT_2 = datetime(2026, 9, 5, 17, 5, tzinfo=timezone.utc)


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
    return DaylogConfig(root=tmp_path, sources=[], user_name="Sam", agent_name="Ari")


def _turn(ts: str, text: str, who: str = USER) -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel="voice", who=who, text=text, session="s1")


def _seed(cfg: DaylogConfig, day_utc: str) -> None:
    append_turns(cfg, [_turn(f"{day_utc}T01:00:00Z", f"question on {day_utc}"),
                       _turn(f"{day_utc}T01:00:05Z", f"answer on {day_utc}", who=AGENT)])


def _run(cfg: DaylogConfig, now: datetime, **kw: object) -> dict[str, object]:
    return run_nightly(cfg, now=now, reindex=False, witness=False, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the run lock


def test_overlapping_runs_one_does_the_work_and_the_other_skips(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, "2026-09-04")
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: FakeProvider())
    assert _run(cfg, NIGHT_1)["ok"] is True
    before = cfg.nightly_status_path.read_text(encoding="utf-8")

    # The second night's run stops inside its digest call until released.
    _seed(cfg, "2026-09-05")
    inside, release = threading.Event(), threading.Event()

    class Held(FakeProvider):
        def call(self, request: LLMRequest) -> LLMResponse:
            inside.set()
            assert release.wait(30), "the test never released the first run"
            return super().call(request)

    held = Held()
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: held)
    first: dict[str, object] = {}
    worker = threading.Thread(target=lambda: first.update(_run(cfg, NIGHT_2)))
    worker.start()
    try:
        assert inside.wait(30), "the first run never reached its digest"
        second = _run(cfg, NIGHT_2, lock_wait_s=0.2)
        # The second run did nothing, and says it skipped: not a failure.
        assert second["skipped"] is True and second["ok"] is True
        assert second["reason"] == "another nightly is running"
        assert second["steps"] == {} and second["llm_attempts"] == []
        # It left the status file alone: still the last complete night's.
        assert cfg.nightly_status_path.read_text(encoding="utf-8") == before
    finally:
        release.set()
        worker.join(60)
    assert not worker.is_alive()
    assert first["ok"] is True and "skipped" not in first
    saved = json.loads(cfg.nightly_status_path.read_text(encoding="utf-8"))
    assert saved["started"] == first["started"]
    assert saved["steps"]["digest"]["days"] == ["2026-09-05"]
    assert len(held.requests) == 1  # one run's digest, not two


def test_a_second_run_waits_for_one_that_is_finishing(
        tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, "2026-09-04")
    result: dict[str, object] = {}
    with exclusive_lock(run_lock_key(cfg.root), wait_s=1) as held:
        assert held
        worker = threading.Thread(target=lambda: result.update(_run(cfg, NIGHT_1,
                                                                    lock_wait_s=20)))
        worker.start()
        worker.join(0.5)
        assert worker.is_alive()  # waiting for the lock, not skipping
    worker.join(60)
    assert "skipped" not in result and result["ok"] is True
    assert cfg.day_digest("2026-09-04").exists()


def test_runs_for_different_roots_do_not_exclude_each_other(
        tmp_path: Path, fake_llm: FakeProvider) -> None:
    other = _cfg(tmp_path / "other")
    _seed(other, "2026-09-04")
    with exclusive_lock(run_lock_key(tmp_path / "busy"), wait_s=1) as held:
        assert held
        status = _run(other, NIGHT_1, lock_wait_s=0.1)
    assert "skipped" not in status and status["ok"] is True


def test_a_run_lock_that_cannot_be_opened_does_not_stop_the_night(
        tmp_path: Path, fake_llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """No lock file can be opened at all: the night runs as it did before the lock."""

    @contextmanager
    def unavailable(key: object, wait_s: float = 0) -> Iterator[str]:
        yield UNAVAILABLE

    monkeypatch.setattr(nightly, "try_lock", unavailable)
    cfg = _cfg(tmp_path)
    _seed(cfg, "2026-09-04")
    status = _run(cfg, NIGHT_1)
    assert status["ok"] is True and status["run_lock"] == "unavailable"
    assert json.loads(cfg.nightly_status_path.read_text(encoding="utf-8"))["ok"] is True


def test_the_cli_reports_a_skipped_run_as_success(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from smriti import cli

    monkeypatch.setattr(nightly, "run_nightly", lambda **kw: {
        "target_day": "2026-09-04", "skipped": True, "ok": True,
        "reason": "another nightly is running", "steps": {}})
    args = argparse.Namespace(rollup_cap=2, digest_cap=10, no_reindex=True, no_witness=True)
    assert cli._cmd_nightly(args) == 0
    assert "Nightly skipped for 2026-09-04: another nightly is running" in capsys.readouterr().out

"""The morning claims its day before composing, so it sends at most once (H11.3).

Delivery is faked (``notify_user`` is replaced), and so is the compose call.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.daylog import morning
from smriti.daylog.config import DaylogConfig
from smriti.daylog.morning import run_morning
from smriti.llm.types import LLMError

NOW = datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)  # 07:00 on 2026-09-05 at UTC+10
DAY = "2026-09-05"


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The messages delivered, in order."""
    sent: list[str] = []
    lock = threading.Lock()

    def fake_notify(cfg: DaylogConfig, message: str) -> None:
        with lock:
            sent.append(message)

    monkeypatch.setattr(morning, "notify_user", fake_notify)
    return sent


@pytest.fixture
def composes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every compose call's user prompt; each returns "good morning"."""
    calls: list[str] = []

    def fake_compose(system: str, user: str, attempts_log: list[dict[str, str]],
                     **kw: object) -> str:
        calls.append(user)
        return "good morning"

    monkeypatch.setattr(morning, "compose", fake_compose)
    return calls


def _cfg(tmp_path: Path) -> DaylogConfig:
    return DaylogConfig(root=tmp_path, sources=[], notify_cmd=["unused"],
                        user_name="Sam", agent_name="Ari")


def _ledger(cfg: DaylogConfig) -> Path:
    return cfg.morning_ledger_dir / DAY


def _write_claim(path: Path, *, age_s: float, token: str = "crashed-run") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": "claimed", "token": token, "pid": 999_999,
                                "claimed_at": time.time() - age_s}), encoding="utf-8")


def test_two_overlapping_mornings_send_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sends: list[str],
) -> None:
    """Both runs start together; one composes (slowly) and the other must
    stop at once instead of composing too. The old check-then-write-after-
    compose let both through."""
    cfg = _cfg(tmp_path)
    start = threading.Barrier(2, timeout=10)
    composing = threading.Semaphore(0)
    finish = threading.Event()
    returned = threading.Semaphore(0)
    results: list[dict[str, object]] = []

    def slow_compose(system: str, user: str, attempts_log: list[dict[str, str]],
                     **kw: object) -> str:
        composing.release()
        assert finish.wait(timeout=10)
        return "good morning"

    monkeypatch.setattr(morning, "compose", slow_compose)

    def run() -> None:
        start.wait()
        try:
            results.append(run_morning(cfg, now=NOW))
        finally:
            returned.release()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert composing.acquire(timeout=10)  # one run is composing...
        other_stopped = returned.acquire(timeout=5)  # ...and the other has returned
        second_compose = composing.acquire(timeout=0.2)
    finally:
        finish.set()
        for thread in threads:
            thread.join(timeout=10)
    assert other_stopped and not second_compose
    assert sends == ["good morning"]
    assert sorted(bool(r["sent"]) for r in results) == [False, True]
    loser = next(r for r in results if not r["sent"])
    assert "claimed by another run" in str(loser["reason"])
    assert json.loads(_ledger(cfg).read_text(encoding="utf-8"))["state"] == "sent"


def test_a_failed_compose_gives_the_day_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sends: list[str],
) -> None:
    cfg = _cfg(tmp_path)

    def seat_down(system: str, user: str, attempts_log: list[dict[str, str]],
                  **kw: object) -> str:
        raise LLMError("all providers failed")

    monkeypatch.setattr(morning, "compose", seat_down)
    with pytest.raises(LLMError):
        run_morning(cfg, now=NOW)
    assert not _ledger(cfg).exists()  # released, not left to go stale
    monkeypatch.setattr(morning, "compose", lambda *a, **kw: "good morning")
    assert run_morning(cfg, now=NOW)["sent"] is True
    assert sends == ["good morning"]


def test_a_crashed_runs_stale_claim_is_taken_over(
    tmp_path: Path, sends: list[str], composes: list[str],
) -> None:
    cfg = _cfg(tmp_path)
    _write_claim(_ledger(cfg), age_s=morning._CLAIM_STALE_S + 60)
    result = run_morning(cfg, now=NOW)
    assert result["sent"] is True and sends == ["good morning"]
    entry = json.loads(_ledger(cfg).read_text(encoding="utf-8"))
    assert entry["state"] == "sent" and entry["token"] != "crashed-run"


def test_a_claim_left_empty_by_a_crash_goes_stale_by_its_age(
    tmp_path: Path, sends: list[str], composes: list[str],
) -> None:
    """A run that crashed between creating the entry and writing it leaves an
    empty file; its modification time stands in for the claim's."""
    cfg = _cfg(tmp_path)
    ledger = _ledger(cfg)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("", encoding="utf-8")
    assert "claimed by another run" in str(run_morning(cfg, now=NOW)["reason"])
    old = time.time() - morning._CLAIM_STALE_S - 60
    os.utime(ledger, (old, old))
    assert run_morning(cfg, now=NOW)["sent"] is True
    assert sends == ["good morning"]


def test_a_fresh_claim_is_left_alone(
    tmp_path: Path, sends: list[str], composes: list[str],
) -> None:
    cfg = _cfg(tmp_path)
    _write_claim(_ledger(cfg), age_s=60)
    before = _ledger(cfg).read_text(encoding="utf-8")
    result = run_morning(cfg, now=NOW)
    assert result["sent"] is False and "claimed by another run" in str(result["reason"])
    assert sends == [] and composes == []
    assert _ledger(cfg).read_text(encoding="utf-8") == before


def test_a_run_whose_claim_was_taken_over_does_not_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sends: list[str],
) -> None:
    """A run that outlived its claim: another run took the day over while it
    composed, so the other run sends and this one must not."""
    cfg = _cfg(tmp_path)

    def compose_and_lose_the_claim(system: str, user: str,
                                   attempts_log: list[dict[str, str]], **kw: object) -> str:
        _write_claim(_ledger(cfg), age_s=0, token="the-other-run")
        return "good morning"

    monkeypatch.setattr(morning, "compose", compose_and_lose_the_claim)
    result = run_morning(cfg, now=NOW)
    assert result["sent"] is False and "taken over" in str(result["reason"])
    assert sends == []
    assert json.loads(_ledger(cfg).read_text(encoding="utf-8"))["token"] == "the-other-run"


@pytest.mark.parametrize("entry", [
    '{"composed_at": "2026-09-05T07:00:00+10:00", "chars": 12}',  # before claims existed
    '{"state": "sending", "token": "t", "composed_at": "x", "chars": 12}',  # crashed mid-send
    '{"state": "sent", "token": "t"}',
    '{"state": "undelivered", "token": "t"}',
    "{not json",  # unreadable: it might have said "sending"
])
def test_a_finished_or_unreadable_entry_is_never_taken_over(
    tmp_path: Path, sends: list[str], composes: list[str], entry: str,
) -> None:
    """However old: a crash after "sending" means a silent morning, never two."""
    cfg = _cfg(tmp_path)
    ledger = _ledger(cfg)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(entry, encoding="utf-8")
    old = time.time() - 24 * 3600
    os.utime(ledger, (old, old))
    result = run_morning(cfg, now=NOW)
    assert result["sent"] is False and str(result["reason"]).startswith("ledger entry")
    assert sends == [] and composes == []
    assert ledger.read_text(encoding="utf-8") == entry

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
from smriti.daylog.render import input_hash, read_input_hash
from smriti.daylog.writer import append_turns, read_whole_day
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


def test_a_run_lock_that_cannot_be_opened_skips_the_night_and_says_why(
        tmp_path: Path, fake_llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """No lock file can be opened at all: nothing can exclude a second run, so this one
    does no work (H14.2, Codex in review #7; it ran unexcluded before). It fails, and
    its status, which the wake reads, says why."""
    from smriti.wake.briefing import _nightly_line

    @contextmanager
    def unavailable(key: object, wait_s: float = 0) -> Iterator[str]:
        yield UNAVAILABLE

    monkeypatch.setattr(nightly, "try_lock", unavailable)
    cfg = _cfg(tmp_path)
    _seed(cfg, "2026-09-04")
    status = _run(cfg, NIGHT_1)
    assert status["skipped"] is True and status["ok"] is False
    assert status["run_lock"] == "unavailable" and "run lock" in str(status["reason"])
    assert status["steps"] == {} and fake_llm.requests == []
    assert not cfg.day_md("2026-09-04").exists() and not cfg.day_digest("2026-09-04").exists()
    saved = json.loads(cfg.nightly_status_path.read_text(encoding="utf-8"))
    assert saved["ok"] is False and saved["reason"] == status["reason"]
    line = _nightly_line(cfg.root)
    assert line.startswith("--- WHILE YOU SLEPT: NIGHTLY SKIPPED for 2026-09-04: the run lock")


def test_the_cli_exits_nonzero_for_a_night_skipped_without_a_lock(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from smriti import cli

    def skipped(**kw: object) -> dict[str, object]:
        return {"skipped": True, "ok": False, "target_day": "2026-09-04", "reason": "no lock"}

    def busy(**kw: object) -> dict[str, object]:
        return {"skipped": True, "ok": True, "target_day": "2026-09-04",
                "reason": "another nightly is running"}

    args = argparse.Namespace(rollup_cap=2, digest_cap=10, no_reindex=True, no_witness=True)
    monkeypatch.setattr(nightly, "run_nightly", skipped)
    assert cli._cmd_nightly(args) == 1
    monkeypatch.setattr(nightly, "run_nightly", busy)
    assert cli._cmd_nightly(args) == 0
    assert "another nightly is running" in capsys.readouterr().out


def _in_threads(n: int, work: object) -> list[BaseException]:
    """Run ``work()`` in *n* threads released together; the exceptions they raised."""
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def one() -> None:
        barrier.wait()
        try:
            work()  # type: ignore[operator]
        except BaseException as exc:  # noqa: BLE001 — collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return errors


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if "tmp" in p.name)


# ---------------------------------------------------------------- unique temp names


def test_concurrent_status_writes_never_share_a_temp_file(tmp_path: Path) -> None:
    """The temp name was the current second, so two writers in one second collided."""
    path = tmp_path / "log" / ".nightly-status.json"
    errors = _in_threads(8, lambda: nightly._write_status(path, {"ok": True, "n": 1}))
    assert errors == []
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True, "n": 1}
    assert _leftovers(path.parent) == []


def test_concurrent_renders_digests_and_rollups_each_use_their_own_temp_file(
        tmp_path: Path, fake_llm: FakeProvider) -> None:
    from datetime import date

    from smriti.daylog.render import render_day
    from smriti.daylog.rollup import build_rollup, enumerate_stale_periods

    cfg = _cfg(tmp_path)
    for day in ("2026-09-01", "2026-09-02"):
        _seed(cfg, day)
    month = cfg.day_md("2026-09-01").parent
    assert _in_threads(6, lambda: render_day(cfg, "2026-09-01")) == []
    assert _in_threads(6, lambda: render_day(cfg, "2026-09-02")) == []
    assert _in_threads(6, lambda: digest_mod.digest_day(cfg, "2026-09-01", [])) == []
    digest_mod.digest_day(cfg, "2026-09-02", [])
    [week] = [p for p in enumerate_stale_periods(cfg, date(2026, 9, 20)) if p.kind == "week"]
    assert _in_threads(6, lambda: build_rollup(week, [], cfg)) == []
    assert _leftovers(month) == []
    assert "test digest" in (month / "week1.md").read_text(encoding="utf-8")


def test_concurrent_witness_proofs_each_use_their_own_temp_file(tmp_path: Path) -> None:
    pytest.importorskip("opentimestamps")
    from smriti import witness

    class Proof:
        def serialize(self, ctx: object) -> None:
            ctx.write_bytes(b"proof bytes")  # type: ignore[attr-defined]

    path = tmp_path / "2026-09-04.txt.ots"
    assert _in_threads(8, lambda: witness._save(Proof(), path)) == []
    assert path.read_bytes() == b"proof bytes"
    assert _leftovers(tmp_path) == []


# ---------------------------------------------------------------- one snapshot per day

DAY = "2026-09-04"
RACED = "a turn appended between the render and the digest"


def _race_between_render_and_digest(cfg: DaylogConfig,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Append a turn to the day just before the nightly's digest of it begins, once."""
    real = nightly.digest_day
    done: list[bool] = []

    def racing(cfg_: DaylogConfig, day: str, attempts: list[dict[str, str]],
               **kw: object) -> Path | None:
        if not done:
            done.append(True)
            append_turns(cfg, [_turn(f"{day}T03:00:00Z", RACED)])
        return real(cfg_, day, attempts, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(nightly, "digest_day", racing)


def _current(cfg: DaylogConfig, day: str = DAY) -> str:
    return input_hash(read_whole_day(cfg.day_jsonl(day)))


def test_a_turn_appended_between_render_and_digest_leaves_the_digest_stale(
        tmp_path: Path, fake_llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _race_between_render_and_digest(cfg, monkeypatch)
    assert _run(cfg, NIGHT_1)["ok"] is True
    # The digest hashes the turns it summarised, as the render does: the raced
    # turn is in neither, so both are stale. It used to hash the turns as they
    # were when the digest began, raced turn included, while summarising the
    # render made before it: a current-looking digest without the turn.
    assert RACED not in fake_llm.requests[0].user
    assert read_input_hash(cfg.day_digest(DAY)) == read_input_hash(cfg.day_md(DAY))
    assert read_input_hash(cfg.day_digest(DAY)) != _current(cfg)
    monkeypatch.setattr(nightly, "digest_day", digest_mod.digest_day)

    # The next night redoes it, and the turn reaches the digest.
    again = _run(cfg, NIGHT_1.replace(minute=30))
    assert again["steps"]["digest"]["days"] == [DAY]  # type: ignore[index]
    assert RACED in fake_llm.requests[-1].user
    assert read_input_hash(cfg.day_digest(DAY)) == _current(cfg)


def test_a_raced_digest_is_redone_even_after_a_night_that_only_renders(
        tmp_path: Path, fake_llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """A render alone (the day past the digest cap) must not make the raced digest current."""
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _race_between_render_and_digest(cfg, monkeypatch)
    _run(cfg, NIGHT_1)
    monkeypatch.setattr(nightly, "digest_day", digest_mod.digest_day)

    render_only = _run(cfg, NIGHT_1.replace(minute=30), digest_cap=0)
    assert render_only["steps"]["render"]["days"] == [DAY]  # type: ignore[index]
    assert render_only["steps"]["digest"]["days"] == []  # type: ignore[index]
    assert read_input_hash(cfg.day_md(DAY)) == _current(cfg)

    last = _run(cfg, NIGHT_1.replace(minute=45))
    assert last["steps"]["digest"]["days"] == [DAY]  # type: ignore[index]
    assert RACED in fake_llm.requests[-1].user


def test_the_digest_reads_the_snapshot_exactly_as_it_read_the_rendered_file(
        tmp_path: Path, fake_llm: FakeProvider) -> None:
    """Digesting the snapshot sends the model the same text as reading DD.md did."""
    cfg = _cfg(tmp_path)
    append_turns(cfg, [
        _turn(f"{DAY}T01:00:00Z", "a question\r\nover two lines\rand a third"),
        _turn(f"{DAY}T01:00:05Z", "an answer with a key sk-ant-api03-" + "A" * 40, who=AGENT),
    ])
    _run(cfg, NIGHT_1)
    through_snapshot = fake_llm.requests[-1].user
    digest_mod.digest_day(cfg, DAY, [])  # reads DD.md, as every digest did before
    assert fake_llm.requests[-1].user == through_snapshot
    assert "sk-ant-api03-" + "A" * 40 not in through_snapshot  # the masked render


def test_a_digest_without_a_snapshot_records_the_hash_of_the_render_it_read(
        tmp_path: Path, fake_llm: FakeProvider) -> None:
    from smriti.daylog.render import render_day

    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    render_day(cfg, DAY)
    append_turns(cfg, [_turn(f"{DAY}T03:00:00Z", RACED)])
    digest_mod.digest_day(cfg, DAY, [])
    assert read_input_hash(cfg.day_digest(DAY)) == read_input_hash(cfg.day_md(DAY))
    assert read_input_hash(cfg.day_digest(DAY)) != _current(cfg)


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

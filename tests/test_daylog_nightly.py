"""Nightly-cycle tests: render, digest, rollups, orchestration, morning."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
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
    return DaylogConfig(root=tmp_path, sources=[], user_name="Sam", agent_name="Ari")


def _turn(ts: str, text: str, who: str = USER, channel: str = "voice",
          session: str = "s1") -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel=channel, who=who, text=text, session=session)


def _seed_day(cfg: DaylogConfig, day_utc: str = "2026-09-04") -> None:
    append_turns(cfg, [
        _turn(f"{day_utc}T01:00:00Z", "morning question"),
        _turn(f"{day_utc}T01:00:05Z", "morning answer", who=AGENT),
        _turn(f"{day_utc}T02:00:00Z", "loop iteration output", who=AGENT,
              channel="claude:smriti", session="auto1"),
    ])


# ---------------------------------------------------------------- render


def test_render_writes_timeline_with_hash_and_autonomous_marker(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    out = render_day(cfg, "2026-09-04")
    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert "**Sam:** morning question" in text
    assert "**Ari:** morning answer" in text
    # The zero-human session is a derived marker, not inline turns.
    assert "[autonomous: claude:smriti" in text
    assert "loop iteration output" not in text
    turns = read_day_turns(cfg.day_jsonl("2026-09-04"))
    assert read_input_hash(out) == input_hash(turns)


def test_render_names_default_to_user_and_agent(tmp_path: Path) -> None:
    cfg = DaylogConfig(root=tmp_path, sources=[])
    _seed_day(cfg)
    text = render_day(cfg, "2026-09-04").read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "**User:** morning question" in text and "**Agent:** morning answer" in text


# A day captured before the speakers became roles: "who" holds the reference
# installation's names. Ids are as stored; the last two turns are one
# unattended session.
_LEGACY_DAY = "\n".join(json.dumps(r) for r in [
    {"id": "claude:abcdef12:aaaaaaaaaaaa", "ts": "2026-11-02T21:05:00Z",
     "channel": "claude:proj", "who": "suti", "text": "Morning. Can you look at the index?",
     "session": "abcdef1234"},
    {"id": "claude:abcdef12:bbbbbbbbbbbb", "ts": "2026-11-02T21:06:10Z",
     "channel": "claude:proj", "who": "narada",
     "text": "Yes. Two things:\n1. stale rows\r\n2. a lock", "session": "abcdef1234"},
    {"id": "voice:20261103:cccccccccccc", "ts": "2026-11-03T02:00:00Z", "channel": "voice",
     "who": "suti", "text": "Remind me about the parcel", "session": "20261103-120000"},
    {"id": "voice:20261103:dddddddddddd", "ts": "2026-11-03T02:00:05Z", "channel": "voice",
     "who": "narada", "text": "Noted.", "session": "20261103-120000"},
    {"id": "codex:feed1234:eeeeeeeeeeee", "ts": "2026-11-03T05:00:00Z",
     "channel": "codex:auto", "who": "narada", "text": "autonomous step one",
     "session": "feed1234"},
    {"id": "codex:feed1234:ffffffffffff", "ts": "2026-11-03T05:20:00Z",
     "channel": "codex:auto", "who": "narada", "text": "autonomous step two",
     "session": "feed1234"},
]) + "\n"

# What render_day wrote for _LEGACY_DAY before the speakers became roles
# (recorded from a run of that code, UTC+10).
_LEGACY_RENDER = """---
date: 2026-11-03
input_hash: a1d98fd4f3012c12
generated_by: smriti nightly (render)
---

# Day log — 2026-11-03

- `07:05` [claude:proj] **Suti:** Morning. Can you look at the index?
- `07:06` **Narada:** Yes. Two things:
  1. stale rows
  2. a lock
- `12:00` [voice] **Suti:** Remind me about the parcel
- `12:00` **Narada:** Noted.

## Autonomous sessions

- [autonomous: codex:auto, 15:00–15:20, 2 turns]
"""


def test_a_legacy_day_renders_byte_identically_with_the_reference_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from smriti.daylog.config import load_config

    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
    (tmp_path / ".smriti").mkdir()
    (tmp_path / ".smriti" / "daylog.json").write_text(
        json.dumps({"user_name": "Suti", "agent_name": "Narada"}), encoding="utf-8")
    cfg = load_config(tmp_path)
    path = cfg.day_jsonl("2026-11-03")
    path.parent.mkdir(parents=True)
    path.write_text(_LEGACY_DAY, encoding="utf-8")
    out = render_day(cfg, "2026-11-03")
    assert out is not None
    # render_day writes in text mode, so the platform's line ending is part of the bytes.
    assert out.read_bytes() == _LEGACY_RENDER.replace("\n", os.linesep).encode("utf-8")
    turns = read_day_turns(path)
    assert [t.who for t in turns] == [USER, AGENT, USER, AGENT, AGENT, AGENT]
    assert input_hash(turns) == "a1d98fd4f3012c12"  # unchanged: past digests stay current


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
    assert fake_llm.requests[0].cli_args == [
        "--strict-mcp-config", "--tools=", "--disallowedTools=*", "--disable-slash-commands"]


class Boom:
    """A provider that is there and fails every call."""

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("primary down")


def _claude_down(monkeypatch: pytest.MonkeyPatch, fallback: FakeProvider) -> None:
    providers = {"claude_cli": Boom(), "codex_cli": fallback}
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: providers[name])


def test_digest_falls_back_when_primary_fails_and_a_fallback_is_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    cfg.nightly_fallback = "codex_cli"  # as the reference install's daylog.json sets
    _seed_day(cfg)
    render_day(cfg, "2026-09-04")
    good = FakeProvider("fallback digest")
    _claude_down(monkeypatch, good)
    attempts: list[dict[str, str]] = []
    out = digest_mod.digest_day(cfg, "2026-09-04", attempts)
    assert out is not None and "fallback digest" in out.read_text(encoding="utf-8")
    assert [a["provider"] for a in attempts] == ["claude_cli", "codex_cli"]
    assert attempts[0]["outcome"].startswith("error")
    # The same call the nightly's codex seat always got: no extra flags.
    assert good.requests[0].cli_args is None


def test_by_default_a_failed_digest_does_not_fall_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H12.2: codex's read-only sandbox can read the user's files while it
    reads untrusted transcripts, so the fallback is opt-in."""
    from smriti.llm.types import LLMError

    cfg = _cfg(tmp_path)
    _seed_day(cfg)
    render_day(cfg, "2026-09-04")
    codex = FakeProvider("must not run")
    _claude_down(monkeypatch, codex)
    attempts: list[dict[str, str]] = []
    with pytest.raises(LLMError, match="all providers failed"):
        digest_mod.digest_day(cfg, "2026-09-04", attempts)
    assert [a["provider"] for a in attempts] == ["claude_cli"] and codex.requests == []
    assert not cfg.day_digest("2026-09-04").exists()


@pytest.mark.parametrize(("env", "configured", "chain"), [
    (None, "", ["claude_cli"]),
    (None, "codex_cli", ["claude_cli", "codex_cli"]),
    ("", "codex_cli", ["claude_cli"]),  # the variable wins; empty means none
    ("codex_cli", "", ["claude_cli", "codex_cli"]),
    (None, "claude_cli", ["claude_cli"]),  # the primary isn't its own fallback
])
def test_the_fallback_is_opt_in_and_the_variable_wins(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        env: str | None, configured: str, chain: list[str]) -> None:
    if env is not None:
        monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", env)
    cfg = _cfg(tmp_path)
    cfg.nightly_fallback = configured
    assert digest_mod._providers(cfg) == chain
    assert digest_mod._providers(None) == (["claude_cli", "codex_cli"]
                                           if env == "codex_cli" else ["claude_cli"])


def test_the_codex_seat_is_read_only_with_no_mcp_from_the_empty_workdir(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """What the fallback call runs: the provider's own isolation and nothing else."""
    import subprocess

    from smriti.llm.providers import codex_cli
    from smriti.llm.workdir import llm_workdir

    seen: dict[str, object] = {}

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.update(cmd=cmd, input=kw.get("input"))
        return subprocess.CompletedProcess(cmd, 0, stdout="a digest", stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    provider = codex_cli.CodexCliProvider()
    response = provider.call(LLMRequest(system="sys", user="the day", cli_args=None))
    assert response.text == "a digest"
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[1:] == ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                       "-c", "mcp_servers={}", "--cd", str(llm_workdir()), "-"]
    assert seen["input"] == "sys\n\nthe day"


def test_daylog_json_names_the_fallback(tmp_path: Path) -> None:
    from smriti.daylog.config import load_config

    assert load_config(tmp_path).nightly_fallback == ""
    path = tmp_path / ".smriti" / "daylog.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"nightly_fallback": " codex_cli "}), encoding="utf-8")
    assert load_config(tmp_path).nightly_fallback == "codex_cli"
    path.write_text(json.dumps({"nightly_fallback": True}), encoding="utf-8")
    assert load_config(tmp_path).nightly_fallback == ""  # a name, or nothing


def test_a_rollup_follows_the_fallback_setting(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from smriti.daylog.rollup import build_rollup
    from smriti.llm.types import LLMError

    cfg = _cfg(tmp_path)
    for day in ("2026-09-01", "2026-09-02"):
        _seed_day(cfg, day)
        render_day(cfg, day)
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: FakeProvider())
    for day in ("2026-09-01", "2026-09-02"):
        digest_mod.digest_day(cfg, day, [])
    [week] = [p for p in enumerate_stale_periods(cfg, date(2026, 9, 20)) if p.kind == "week"]
    good = FakeProvider("fallback rollup")
    _claude_down(monkeypatch, good)
    with pytest.raises(LLMError):
        build_rollup(week, [], cfg)  # off by default
    cfg.nightly_fallback = "codex_cli"
    attempts: list[dict[str, str]] = []
    out = build_rollup(week, attempts, cfg)
    assert "fallback rollup" in out.read_text(encoding="utf-8")
    assert [a["provider"] for a in attempts] == ["claude_cli", "codex_cli"]


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

    out = build_rollup(stale[0], [], cfg)  # as the nightly builds it: the prompt names Ari
    assert out.name == "week1.md"
    # Now up to date; a repaired member digest makes it stale again.
    assert enumerate_stale_periods(cfg, today) == []
    append_turns(cfg, [_turn("2026-09-01T03:00:00Z", "late arrival")])
    render_day(cfg, "2026-09-01")
    digest_mod.digest_day(cfg, "2026-09-01", [])
    assert [p.out_path.name for p in enumerate_stale_periods(cfg, today)] == ["week1.md"]


def test_the_rollup_prompt_names_the_configured_agent(tmp_path: Path) -> None:
    from smriti.daylog.rollup import _system_prompt

    assert _system_prompt("week", _cfg(tmp_path)).startswith("You are Ari's memory rollup")
    assert _system_prompt("year").startswith("You are Agent's memory rollup writer")
    reference = DaylogConfig(root=tmp_path, user_name="Suti", agent_name="Narada")
    assert _system_prompt("week", reference) == (
        "You are Narada's memory rollup writer. You receive the digests that make up one "
        "week. Compress them into the week's highlights: what happened, what was decided, "
        "what changed, what stayed open. Markdown, specific, no preamble. Keep it to "
        "roughly a third of the input length.")


def test_rollup_body_keeps_every_input_of_a_long_year(tmp_path: Path) -> None:
    from smriti.daylog.rollup import _BUDGET, Period, rollup_body

    months = []
    for m in range(1, 13):
        path = tmp_path / "2026" / f"{m:02d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Month {m:02d}\n" + "x" * 30_000 + f"\nEND-{m:02d}\n", encoding="utf-8")
        months.append(path)
    body = rollup_body(Period("year", tmp_path / "2026.md", months))
    # Every month is represented (a flat cut used to drop July onwards) ...
    for m in range(1, 13):
        assert f"# Month {m:02d}" in body
    # ... each long input is marked as truncated, and the total stays in budget.
    assert body.count("[... truncated for length]") == 12
    assert len(body) <= _BUDGET["year"] + 12 * 40


def test_rollup_body_leaves_short_inputs_whole(tmp_path: Path) -> None:
    from smriti.daylog.rollup import Period, rollup_body

    days = []
    for d in (1, 2, 3):
        path = tmp_path / f"0{d}-digest.md"
        path.write_text(f"day {d} complete\n", encoding="utf-8")
        days.append(path)
    body = rollup_body(Period("week", tmp_path / "week1.md", days))
    assert "truncated" not in body
    assert all(f"day {d} complete" in body for d in (1, 2, 3))


# ---------------------------------------------------------------- nightly


def test_nightly_renders_digests_and_writes_status(
    tmp_path: Path, fake_llm: FakeProvider
) -> None:
    cfg = _cfg(tmp_path)
    _seed_day(cfg)  # UTC 2026-09-04 => UTC+10 2026-09-04 (early-morning turns)
    # 17:05 UTC = 03:05 UTC+10 on 09-05 => target day is the just-closed 09-04.
    status = run_nightly(
        cfg, witness=False, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc), reindex=False
    )
    assert status["target_day"] == "2026-09-04"
    assert cfg.day_md("2026-09-04").exists()
    assert cfg.day_digest("2026-09-04").exists()
    saved = json.loads(cfg.nightly_status_path.read_text(encoding="utf-8"))
    assert saved["steps"]["digest"]["days"] == ["2026-09-04"]
    assert saved["ok"] is True
    # Second run is a no-op (idempotent by input hash).
    status2 = run_nightly(cfg, witness=False, now=datetime(2026, 9, 4, 17, 10, tzinfo=timezone.utc),
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
    status = run_nightly(cfg, witness=False, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc),
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
    status = run_nightly(cfg, witness=False, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc),
                         reindex=False)
    assert "2026-07-01" in status["steps"]["digest"]["days"]  # type: ignore[index]
    assert DaylogState.load(cfg.state_path).dirty_days == set()


def test_nightly_digest_cap_defers_oldest(tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        _seed_day(cfg, day)
    status = run_nightly(cfg, witness=False, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc),
                         reindex=False, digest_cap=2)
    digest = status["steps"]["digest"]  # type: ignore[index]
    assert digest["days"] == ["2026-09-04", "2026-09-03"]  # newest first
    assert digest["deferred"] == 2
    # Deferred days remain stale; the next night picks them up.
    status2 = run_nightly(cfg, witness=False, now=datetime(2026, 9, 4, 17, 30, tzinfo=timezone.utc),
                          reindex=False, digest_cap=2)
    assert status2["steps"]["digest"]["days"] == ["2026-09-02", "2026-09-01"]  # type: ignore[index]


def test_dirty_marker_survives_when_day_changes_under_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn landing mid-digest renews the marker instead of being lost."""
    from smriti.daylog.state import DaylogState

    cfg = _cfg(tmp_path)
    _seed_day(cfg, "2026-07-01")
    state = DaylogState.load(cfg.state_path)
    state.mark_dirty({"2026-07-01"})
    state.save()

    class RacingProvider(FakeProvider):
        def call(self, request: LLMRequest) -> LLMResponse:
            # Simulate logd appending to the same day mid-digest.
            append_turns(cfg, [_turn("2026-07-01T05:00:00Z", "raced in")])
            return super().call(request)

    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: RacingProvider())
    run_nightly(
        cfg, witness=False, now=datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc), reindex=False
    )
    # The digest is stale relative to the raced-in turn: marker kept.
    assert "2026-07-01" in DaylogState.load(cfg.state_path).dirty_days


def test_claude_cli_fails_closed_without_isolated_workdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smriti.llm.workdir as workdir_mod
    from smriti.llm.providers.claude_cli import ClaudeCliProvider
    from smriti.llm.types import LLMError

    def boom() -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(workdir_mod, "llm_workdir", boom)
    with pytest.raises(LLMError, match="isolated LLM workdir"):
        ClaudeCliProvider().call(LLMRequest(system="s", user="u"))


def test_index_step_retries_through_transient_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import smriti.store.indexer as indexer_mod
    from smriti.daylog.nightly import _index_with_retries

    calls = {"n": 0}

    def flaky(**kwargs: object) -> dict[str, int]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        # The step must run with the raised busy timeout in effect.
        import os
        assert os.environ["SMRITI_DB_BUSY_TIMEOUT_MS"] == "60000"
        return {"scanned": 5, "indexed": 2}

    monkeypatch.setattr(indexer_mod, "index_tree", flaky)
    monkeypatch.setattr("smriti.daylog.nightly.time.sleep", lambda s: None)
    result = _index_with_retries(_cfg(tmp_path))
    assert result["ok"] is True and result["attempts"] == 3 and result["indexed"] == 2
    import os
    assert "SMRITI_DB_BUSY_TIMEOUT_MS" not in os.environ  # restored


def test_index_step_fails_visibly_when_wedged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    import smriti.store.indexer as indexer_mod
    from smriti.daylog.nightly import _INDEX_RETRY_DELAYS_S, _index_with_retries

    def wedged(**kwargs: object) -> dict[str, int]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(indexer_mod, "index_tree", wedged)
    monkeypatch.setattr("smriti.daylog.nightly.time.sleep", lambda s: None)
    result = _index_with_retries(_cfg(tmp_path))
    assert result["ok"] is False
    assert result["attempts"] == len(_INDEX_RETRY_DELAYS_S)
    assert "locked" in str(result["error"])


# ---------------------------------------------------------------- morning


def _notify_to_file(tmp_path: Path) -> tuple[list[str], Path]:
    out = tmp_path / "sent.txt"
    script = f"import sys,pathlib; pathlib.Path(r'{out}').write_text(sys.stdin.read())"
    return [sys.executable, "-c", script], out


def test_morning_sends_once_and_only_once(tmp_path: Path, fake_llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    cfg.notify_cmd, sent = _notify_to_file(tmp_path)
    now = datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)  # 07:00 UTC+10 09-05
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
        _turn("2026-09-04T11:17:00Z", "Is the voice still working?", channel="telegram"),
        _turn("2026-09-04T09:00:00Z", "old turn", channel="voice"),  # outside window
        _turn("2026-09-04T11:18:00Z", "own channel", channel="claude:smriti"),
    ])
    line = presence_line(exclude_channel="claude:smriti", now=now, cfg=cfg)
    assert "telegram" in line and "3 min ago" in line
    assert '(Sam: "Is the voice still working?")' in line
    assert "old turn" not in line and "own channel" not in line


def test_presence_line_empty_on_missing_log(tmp_path: Path) -> None:
    from smriti.daylog.presence import presence_line

    assert presence_line(cfg=_cfg(tmp_path)) == ""


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


def test_morning_status_file_never_carries_exception_text(tmp_path: Path,
                                                          fake_llm: FakeProvider) -> None:
    """Review: a failing notify_cmd's exception string carries its argv,
    which can hold a token; the status file lives in the backed-up log
    tree, so it records the failure CLASS only."""
    cfg = _cfg(tmp_path)
    cfg.notify_cmd = [sys.executable, "-c", "import sys; sys.exit(3)",
                      "--auth", "Bearer-SECRET-123"]
    result = run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))
    assert result["sent"] is False and result["phase"] == "deliver"
    status_text = (cfg.log_dir / ".morning-status.json").read_text(encoding="utf-8")
    assert "SECRET" not in status_text and "Bearer" not in status_text
    status = json.loads(status_text)
    assert status["reason"] == result["reason"]
    assert "message" not in status  # the text itself is the day's business, not status


def test_morning_status_written_when_compose_fails(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    cfg.notify_cmd = [sys.executable, "-c", "pass"]

    def boom(system, user, attempts_log, **kw):  # noqa: ANN001
        attempts_log.append({"provider": "claude_cli", "outcome": "error: exit 1"})
        raise RuntimeError("every provider failed: token=Bearer-SECRET")

    monkeypatch.setattr("smriti.daylog.morning.compose", boom)
    with pytest.raises(RuntimeError):
        run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))
    status_text = (cfg.log_dir / ".morning-status.json").read_text(encoding="utf-8")
    assert "SECRET" not in status_text
    status = json.loads(status_text)
    assert status["phase"] == "compose" and status["reason"] == "RuntimeError"
    assert status["llm_attempts"] == [{"provider": "claude_cli", "outcome": "error: exit 1"}]
    # No ledger: the day is still open for a retry once the seat is back.
    assert not cfg.morning_ledger_dir.exists() or not any(cfg.morning_ledger_dir.iterdir())


def test_morning_status_scrubs_quoted_and_trailing_secrets(tmp_path: Path, monkeypatch) -> None:
    """Recheck: scrubbing must happen on the values, not the JSON text —
    escaping hides a quoted credential, and a trailing match would eat
    the closing quote and leave invalid JSON."""
    cfg = _cfg(tmp_path)
    cfg.notify_cmd = [sys.executable, "-c", "pass"]

    def boom(system, user, attempts_log, **kw):  # noqa: ANN001
        attempts_log.append({"provider": "claude_cli",
                             "outcome": 'error: api_key="sk-EXAMPLE1234567890SECRET"'})
        attempts_log.append({"provider": "codex_cli",
                             "outcome": "error: token=sk-EXAMPLE1234567890SECRET"})
        raise RuntimeError("every provider failed")

    monkeypatch.setattr("smriti.daylog.morning.compose", boom)
    with pytest.raises(RuntimeError):
        run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))
    status_text = (cfg.log_dir / ".morning-status.json").read_text(encoding="utf-8")
    status = json.loads(status_text)  # still valid JSON
    assert "EXAMPLE1234567890SECRET" not in status_text
    assert [a["provider"] for a in status["llm_attempts"]] == ["claude_cli", "codex_cli"]


def test_morning_location_comes_from_private_config(tmp_path: Path) -> None:
    import importlib
    import json

    from smriti.daylog.config import load_config

    morning = importlib.import_module("smriti.daylog.morning")
    assert load_config(tmp_path).location == ""
    assert "{" not in morning._system_prompt(load_config(tmp_path))
    (tmp_path / ".smriti").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".smriti" / "daylog.json").write_text(
        json.dumps({"location": "  Lisbon, Portugal "}), encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.location == "Lisbon, Portugal"
    assert "(in Lisbon, Portugal)" in morning._system_prompt(cfg)
    (tmp_path / ".smriti" / "daylog.json").write_text(
        json.dumps({"location": "Lisbon, Portugal", "morning_web_search": True}),
        encoding="utf-8")
    prompt = morning._system_prompt(load_config(tmp_path))
    assert "(in Lisbon, Portugal)" in prompt and "tech/AI, Lisbon, Portugal" in prompt


def test_morning_web_search_is_off_unless_daylog_json_turns_it_on(tmp_path: Path) -> None:
    from smriti.daylog.config import load_config

    assert load_config(tmp_path).morning_web_search is False
    (tmp_path / ".smriti").mkdir()
    path = tmp_path / ".smriti" / "daylog.json"
    path.write_text(json.dumps({"morning_web_search": "yes"}), encoding="utf-8")
    assert load_config(tmp_path).morning_web_search is False  # only a JSON true
    path.write_text(json.dumps({"morning_web_search": True}), encoding="utf-8")
    assert load_config(tmp_path).morning_web_search is True


def test_a_morning_without_web_search_gets_no_tools_and_offers_no_news(
    tmp_path: Path, fake_llm: FakeProvider,
) -> None:
    """The morning reads the digest, which derives from untrusted transcripts:
    by default it gets the digest's no-tools flags, and a prompt that doesn't
    promise headlines it can't fetch."""
    cfg = _cfg(tmp_path)
    cfg.notify_cmd, _sent = _notify_to_file(tmp_path)
    assert run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))["sent"] is True
    request = fake_llm.requests[0]
    assert request.cli_args == digest_mod._NO_TOOLS_ARGS
    assert "you have no web search" in request.system
    assert "Use web search" not in request.system and "half-line each" not in request.system


def test_a_morning_with_web_search_gets_it_and_nothing_else(
    tmp_path: Path, fake_llm: FakeProvider,
) -> None:
    from smriti.daylog.morning import _COMPOSE_ARGS

    cfg = _cfg(tmp_path)
    cfg.notify_cmd, _sent = _notify_to_file(tmp_path)
    cfg.morning_web_search = True
    assert run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))["sent"] is True
    request = fake_llm.requests[0]
    assert request.cli_args == _COMPOSE_ARGS
    assert "Use web search" in request.system


def test_without_a_profile_the_morning_has_no_persona(tmp_path: Path) -> None:
    from smriti.daylog.morning import _system_prompt

    prompt = _system_prompt(_cfg(tmp_path))
    assert prompt.startswith("You are Ari composing a short good-morning message to Sam. ")
    assert "wandering sage" not in prompt and "Narada" not in prompt and "Suti" not in prompt
    assert "{" not in prompt and "Return ONLY the message." in prompt
    generic = _system_prompt(DaylogConfig(root=tmp_path))
    assert generic.startswith("You are Agent composing a short good-morning message to User.")


def test_a_morning_whose_profile_is_missing_fails_visibly(tmp_path: Path,
                                                          fake_llm: FakeProvider) -> None:
    from smriti.profiles import ProfileError

    cfg = DaylogConfig(root=tmp_path, notify_cmd=["unused"], profile="nosuch")
    with pytest.raises(ProfileError):
        run_morning(cfg, now=datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc))
    status = json.loads((cfg.log_dir / ".morning-status.json").read_text(encoding="utf-8"))
    assert status["sent"] is False and status["phase"] == "compose"
    assert not (cfg.morning_ledger_dir / "2026-09-05").exists()  # tomorrow can still send


def test_morning_dry_run_uses_the_formatted_prompt(monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from smriti import cli
    from smriti.daylog import morning

    seen: dict[str, str] = {}

    def fake_compose(system: str, user: str, attempts: list, **kw: object) -> str:
        seen["system"] = system
        return "ok"

    monkeypatch.setattr(morning, "compose", fake_compose)
    assert cli._cmd_morning(argparse.Namespace(dry_run=True)) == 0
    assert "{where}" not in seen["system"] and "{region}" not in seen["system"]


@pytest.mark.parametrize("web_search", [False, True])
def test_a_morning_dry_run_has_exactly_the_scheduled_runs_tools(
        monkeypatch: pytest.MonkeyPatch, web_search: bool) -> None:
    """Codex review of H11.2: the dry run passed the web-search flags whatever
    morning_web_search said, so a preview could search when the real run couldn't."""
    import argparse
    import json

    from smriti import cli
    from smriti.core.tree import tree_root
    from smriti.daylog import morning
    from smriti.daylog.config import load_config

    config = tree_root() / ".smriti" / "daylog.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"morning_web_search": web_search}), encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_compose(system: str, user: str, attempts: list, **kw: object) -> str:
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(morning, "compose", fake_compose)
    assert cli._cmd_morning(argparse.Namespace(dry_run=True)) == 0
    assert seen["claude_cli_args"] == morning._cli_args(load_config())
    assert ("--tools=WebSearch" in seen["claude_cli_args"]) is web_search  # type: ignore[operator]


def test_a_web_search_call_never_falls_back_to_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call given claude_cli_args reads untrusted web results; codex_cli
    can't be held to the same tool boundary, so it is never the fallback
    (Codex, 2026-11-05)."""
    good = FakeProvider("from codex")

    class Boom:
        def is_available(self) -> bool:
            return True

        def call(self, request: LLMRequest) -> LLMResponse:
            raise RuntimeError("claude down")

    providers = {"claude_cli": Boom(), "codex_cli": good}
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: providers[name])
    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", "codex_cli")  # even with the fallback on
    attempts: list[dict[str, str]] = []
    from smriti.llm.types import LLMError

    with pytest.raises(LLMError, match="all providers failed"):
        digest_mod.compose("sys", "user", attempts, claude_cli_args=["--tools", "WebSearch"])
    assert [a["provider"] for a in attempts] == ["claude_cli"]


# ------------------------------------------------- the morning's fallback (H12.2)

_MORNING = datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc)  # 07:00 on 09-05, UTC+10


def test_by_default_a_morning_whose_compose_fails_sends_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As since d2e0518: no fallback unless the nightly's is on."""
    from smriti.llm.types import LLMError

    cfg = _cfg(tmp_path)
    cfg.notify_cmd, sent = _notify_to_file(tmp_path)
    codex = FakeProvider("must not run")
    _claude_down(monkeypatch, codex)
    with pytest.raises(LLMError):
        run_morning(cfg, now=_MORNING)
    assert codex.requests == [] and not sent.exists()
    status = json.loads((cfg.log_dir / ".morning-status.json").read_text(encoding="utf-8"))
    assert [a["provider"] for a in status["llm_attempts"]] == ["claude_cli"]
    assert status["phase"] == "compose" and status["sent"] is False


@pytest.mark.parametrize("web_search", [False, True], ids=["digest-only", "web-search"])
def test_with_the_fallback_on_the_morning_falls_back_from_the_digest_alone(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, web_search: bool) -> None:
    """The fallback never searches: the digest-only prompt and the codex seat's
    own call, whatever morning_web_search says."""
    cfg = _cfg(tmp_path)
    cfg.notify_cmd, sent = _notify_to_file(tmp_path)
    cfg.nightly_fallback, cfg.morning_web_search = "codex_cli", web_search
    digest = cfg.day_digest("2026-09-04")
    digest.parent.mkdir(parents=True)
    digest.write_text("## Highlights\n- Ari finished the index\n", encoding="utf-8")
    codex = FakeProvider("good morning from codex")
    _claude_down(monkeypatch, codex)
    result = run_morning(cfg, now=_MORNING)
    assert result["sent"] is True and sent.read_text(encoding="utf-8") == codex.text
    [request] = codex.requests
    assert request.cli_args is None  # as the nightly's codex call: its own isolation only
    assert "you have no web search" in request.system and "Use web search" not in request.system
    assert "Ari finished the index" in request.user
    assert [a["provider"] for a in result["llm_attempts"]] == ["claude_cli", "codex_cli"]  # type: ignore[union-attr]


@pytest.mark.parametrize(("env", "configured", "falls_back"), [
    ("", "codex_cli", False),  # the variable wins: empty means none
    ("codex_cli", "", True),
])
def test_the_morning_follows_the_variable_too(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        env: str, configured: str, falls_back: bool) -> None:
    from smriti.daylog.morning import compose_message
    from smriti.llm.types import LLMError

    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", env)
    cfg = _cfg(tmp_path)
    cfg.nightly_fallback = configured
    codex = FakeProvider("from codex")
    _claude_down(monkeypatch, codex)
    attempts: list[dict[str, str]] = []
    if falls_back:
        assert compose_message(cfg, "digest", attempts) == "from codex"
    else:
        with pytest.raises(LLMError):
            compose_message(cfg, "digest", attempts)
    assert [a["provider"] for a in attempts] == ["claude_cli", "codex_cli"][:1 + falls_back]


def test_a_morning_dry_run_falls_back_as_the_scheduled_run_does(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from smriti import cli
    from smriti.core.tree import tree_root

    config = tree_root() / ".smriti" / "daylog.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"nightly_fallback": "codex_cli"}), encoding="utf-8")
    codex = FakeProvider("a sample from codex")
    _claude_down(monkeypatch, codex)
    assert cli._cmd_morning(argparse.Namespace(dry_run=True)) == 0
    assert "a sample from codex" in capsys.readouterr().out

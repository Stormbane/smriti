"""H12.12: a digest's or rollup's freshness follows everything that made it.

A rollup is stale when a member's content changes, not only the turns behind
it; provenance is taken from what was read before the call. Artifacts
written before H12.12 lack the new fields and keep their old rule, so the
upgrade regenerates nothing by itself.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog import rollup as rollup_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.digest import digest_fingerprint
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
from smriti.daylog.nightly import run_nightly
from smriti.daylog.render import head_field, read_head, render_day
from smriti.daylog.rollup import (
    Period,
    build_rollup,
    enumerate_stale_periods,
    rollup_fingerprint,
)
from smriti.daylog.writer import append_turns
from smriti.llm.types import LLMRequest, LLMResponse

TODAY = date(2026, 9, 20)  # September's first two weeks are closed
DAYS = ("2026-09-01", "2026-09-02")


class FakeProvider:
    """Answers with ``text``; records the requests it served."""

    def __init__(self, text: str = "## Highlights\n- test digest") -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider()
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: provider)
    return provider


def _cfg(tmp_path: Path) -> DaylogConfig:
    return DaylogConfig(root=tmp_path, sources=[], user_name="Sam", agent_name="Ari")


def _turn(ts: str, text: str, who: str = USER) -> Turn:
    parsed = parse_ts(ts)
    assert parsed is not None
    return Turn(ts=parsed, channel="voice", who=who, text=text, session="s1")


def _digested(cfg: DaylogConfig, *days: str) -> None:
    for day in days:
        append_turns(cfg, [_turn(f"{day}T01:00:00Z", f"question on {day}"),
                           _turn(f"{day}T01:00:05Z", f"answer on {day}", who=AGENT)])
        render_day(cfg, day)
        digest_mod.digest_day(cfg, day, [])


def _week(cfg: DaylogConfig) -> Period:
    [week] = [p for p in enumerate_stale_periods(cfg, TODAY) if p.kind == "week"]
    return week


def _stale_names(cfg: DaylogConfig) -> list[str]:
    return [p.out_path.name for p in enumerate_stale_periods(cfg, TODAY)]


def _write_old_format_rollup(period: Period) -> None:
    """A rollup as smriti wrote it before H12.12: ``inputs_hash`` and nothing newer."""
    period.out_path.write_text(
        f"---\nkind: {period.kind}-rollup\ninputs_hash: {period.manifest_hash()}\n"
        "generated_by: smriti nightly (rollup)\n---\n## Highlights\n- an old week\n",
        encoding="utf-8")


# ---------------------------------------------------------------- member content


def test_a_digest_regenerated_from_the_same_turns_makes_its_week_stale(
        tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    build_rollup(_week(cfg), [], cfg)
    assert _stale_names(cfg) == []
    llm.text = "## Highlights\n- the same day, told differently"
    digest_mod.digest_day(cfg, DAYS[0], [])  # same turns, so the same input hash
    assert _stale_names(cfg) == ["week1.md"]


def test_a_regenerated_week_makes_its_month_stale(tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    week = _week(cfg)
    build_rollup(week, [], cfg)
    today = date(2026, 10, 2)  # September closed: its month is due
    [month] = [p for p in enumerate_stale_periods(cfg, today) if p.kind == "month"]
    build_rollup(month, [], cfg)
    assert enumerate_stale_periods(cfg, today) == []
    llm.text = "## Highlights\n- the week, told differently"
    build_rollup(week, [], cfg)
    assert [p.out_path.name for p in enumerate_stale_periods(cfg, today)] == ["09.md"]


def test_a_member_changed_during_the_call_leaves_the_rollup_stale(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance is what the call read: it was computed after the call, so a
    digest rewritten meanwhile was recorded as read."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: FakeProvider())
    _digested(cfg, *DAYS)
    week = _week(cfg)
    member = cfg.day_digest(DAYS[0])

    class Rewriting(FakeProvider):
        def call(self, request: LLMRequest) -> LLMResponse:
            member.write_text(member.read_text(encoding="utf-8") + "- added meanwhile\n",
                              encoding="utf-8")
            return super().call(request)

    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: Rewriting())
    build_rollup(week, [], cfg)
    assert _stale_names(cfg) == ["week1.md"]


def test_a_new_rollup_records_the_old_manifest_too(tmp_path: Path, llm: FakeProvider) -> None:
    """An older smriti, which reads only ``inputs_hash``, finds a new rollup current."""
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    week = _week(cfg)
    build_rollup(week, [], cfg)
    head = read_head(week.out_path)
    assert head_field(head, "inputs_hash") == week.manifest_hash()
    assert head_field(head, "members_hash")


# ---------------------------------------------------------------- the upgrade


def test_an_old_format_rollup_is_current_by_its_old_rule(
        tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    week = _week(cfg)
    _write_old_format_rollup(week)
    assert _stale_names(cfg) == []
    # Its rule sees input hashes only: a digest rewritten from the same turns
    # doesn't make it stale, as it never did; nothing is regenerated for lacking
    # the new field.
    llm.text = "## Highlights\n- the same day, told differently"
    digest_mod.digest_day(cfg, DAYS[0], [])
    assert _stale_names(cfg) == []
    assert llm.requests[-1].system.startswith("You are Ari's nightly digest writer")


def test_an_old_format_rollup_whose_member_changed_is_regenerated_in_the_new_format(
        tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    _write_old_format_rollup(_week(cfg))
    append_turns(cfg, [_turn(f"{DAYS[0]}T03:00:00Z", "a late turn")])
    render_day(cfg, DAYS[0])
    digest_mod.digest_day(cfg, DAYS[0], [])  # a new input hash: stale by the old rule
    week = _week(cfg)
    build_rollup(week, [], cfg)
    assert head_field(read_head(week.out_path), "members_hash")
    assert _stale_names(cfg) == []


def _night(cfg: DaylogConfig, now: datetime, **kw: object) -> dict[str, Any]:
    status = run_nightly(cfg, now=now, reindex=False, witness=False, **kw)  # type: ignore[arg-type]
    steps = status["steps"]
    assert isinstance(steps, dict)
    return steps


# ---------------------------------------------------------------- prompt and config

NIGHT = datetime(2026, 9, 4, 17, 5, tzinfo=timezone.utc)  # target day 2026-09-04
DAY = "2026-09-04"


def _seed(cfg: DaylogConfig, day: str) -> None:
    append_turns(cfg, [_turn(f"{day}T01:00:00Z", f"question on {day}"),
                       _turn(f"{day}T01:00:05Z", f"answer on {day}", who=AGENT)])


def _fp(path: Path) -> str:
    return head_field(read_head(path), "prompt_fp")


def _as_written_before_h12_12(path: Path) -> None:
    """Drop the fields H12.12 added: the artifact as an older smriti wrote it."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(ln for ln in lines
                            if not ln.startswith(("prompt_fp:", "members_hash:"))),
                    encoding="utf-8")


def test_a_digest_records_the_fingerprint_of_what_made_it(
        tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, DAYS[0])
    assert _fp(cfg.day_digest(DAYS[0])) == digest_fingerprint(cfg)
    week = _week(cfg)
    build_rollup(week, [], cfg)
    assert _fp(week.out_path) == rollup_fingerprint("week", cfg)


@pytest.mark.parametrize("change", ["agent", "user", "fallback", "provider", "prompt"])
def test_the_fingerprint_follows_the_prompt_the_names_and_the_provider_chain(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    cfg = _cfg(tmp_path)
    digest_fp, week_fp = digest_fingerprint(cfg), rollup_fingerprint("week", cfg)
    assert digest_fingerprint(_cfg(tmp_path)) == digest_fp  # the same config, the same print
    assert rollup_fingerprint("month", cfg) != week_fp  # each kind has its own prompt
    if change == "agent":
        cfg.agent_name = "Ara"
    elif change == "user":
        cfg.user_name = "Sana"
    elif change == "fallback":
        cfg.nightly_fallback = "codex_cli"
    elif change == "provider":
        monkeypatch.setenv("SMRITI_NIGHTLY_PROVIDER", "codex_cli")
    else:
        monkeypatch.setattr(digest_mod, "_MERGE_SYSTEM", digest_mod._MERGE_SYSTEM + " Be brief.")
        monkeypatch.setattr(rollup_mod, "_SYSTEM", rollup_mod._SYSTEM + " Be brief.")
    assert digest_fingerprint(cfg) != digest_fp
    assert rollup_fingerprint("week", cfg) != week_fp


def test_a_changed_prompt_makes_a_digest_stale_and_the_new_one_carries_it(
        tmp_path: Path, llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    assert _night(cfg, NIGHT)["digest"]["days"] == [DAY]
    before = _fp(cfg.day_digest(DAY))
    monkeypatch.setattr(digest_mod, "_SECTIONS", digest_mod._SECTIONS + " Be brief.")
    assert _night(cfg, NIGHT.replace(minute=30))["digest"]["days"] == [DAY]
    assert _fp(cfg.day_digest(DAY)) == digest_fingerprint(cfg) != before
    assert llm.requests[-1].system.endswith("Be brief.")
    assert _night(cfg, NIGHT.replace(minute=45))["digest"]["days"] == []


def test_a_changed_prompt_makes_a_rollup_stale_unless_it_predates_the_fingerprint(
        tmp_path: Path, llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    week = _week(cfg)
    build_rollup(week, [], cfg)
    monkeypatch.setattr(rollup_mod, "_SYSTEM", rollup_mod._SYSTEM + " Be brief.")
    assert _stale_names(cfg) == ["week1.md"]
    _write_old_format_rollup(week)
    assert _stale_names(cfg) == []


def test_an_old_format_digest_is_not_redone_for_lacking_the_fingerprint(
        tmp_path: Path, llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _night(cfg, NIGHT)
    _as_written_before_h12_12(cfg.day_digest(DAY))
    served = len(llm.requests)
    # The prompt and the config change too: an old digest records neither.
    monkeypatch.setattr(digest_mod, "_SECTIONS", digest_mod._SECTIONS + " Be brief.")
    cfg.nightly_fallback = "codex_cli"
    steps = _night(cfg, NIGHT.replace(minute=30))
    assert steps["render"]["days"] == [] and steps["digest"]["days"] == []
    assert len(llm.requests) == served and _fp(cfg.day_digest(DAY)) == ""

    # A day that changes is redone for that, and its digest carries the print.
    append_turns(cfg, [_turn(f"{DAY}T03:00:00Z", "a late turn")])
    assert _night(cfg, NIGHT.replace(minute=45))["digest"]["days"] == [DAY]
    assert _fp(cfg.day_digest(DAY)) == digest_fingerprint(cfg)


def test_the_first_night_on_new_code_redoes_nothing_in_a_tree_written_before_it(
        tmp_path: Path, llm: FakeProvider) -> None:
    """A tree whose digests and rollups an older smriti wrote: no digest and no rollup
    is regenerated for the new fields, only for what the old rules see."""
    cfg = _cfg(tmp_path)
    days = ("2026-09-01", "2026-09-02", "2026-09-09", "2026-09-10", "2026-10-01")
    for day in days:
        _digested(cfg, day)
        _as_written_before_h12_12(cfg.day_digest(day))
    target = date(2026, 10, 2)
    while stale := enumerate_stale_periods(cfg, target):  # weeks, then their month
        for period in stale:
            _write_old_format_rollup(period)
    served = len(llm.requests)
    now = datetime(2026, 10, 2, 17, 5, tzinfo=timezone.utc)  # target day 2026-10-02

    cfg.nightly_fallback = "codex_cli"  # as the reference install turns it on (H12.2)
    steps = _night(cfg, now, rollup_cap=10)
    assert steps["render"]["days"] == [] and steps["digest"]["days"] == []
    assert steps["rollups"]["built"] == [] and steps["rollups"]["remaining"] == 0
    assert len(llm.requests) == served

    # A late turn redoes its day, then its week by the week's old rule, and the
    # week's new file its month on the night after, as before.
    append_turns(cfg, [_turn("2026-09-09T03:00:00Z", "a late turn")])
    steps = _night(cfg, now.replace(minute=30), rollup_cap=10)
    assert steps["digest"]["days"] == ["2026-09-09"]
    assert steps["rollups"]["built"] == ["week2.md"]
    assert _night(cfg, now.replace(minute=45), rollup_cap=10)["rollups"]["built"] == ["09.md"]
    assert _night(cfg, now.replace(minute=55), rollup_cap=10)["rollups"]["built"] == []


def test_an_old_format_month_follows_its_weeks_files(tmp_path: Path, llm: FakeProvider) -> None:
    """The old rule hashes a member rollup's file: a regenerated week still reaches its month."""
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    week = _week(cfg)
    _write_old_format_rollup(week)
    today = date(2026, 10, 2)
    [month] = [p for p in enumerate_stale_periods(cfg, today) if p.kind == "month"]
    _write_old_format_rollup(month)
    assert enumerate_stale_periods(cfg, today) == []
    build_rollup(week, [], cfg)  # regenerated, for whatever reason
    assert [p.out_path.name for p in enumerate_stale_periods(cfg, today)] == ["09.md"]

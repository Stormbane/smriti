"""H12.12: a digest's or rollup's freshness follows everything that made it.

A rollup is stale when a member's content changes, not only the turns behind
it; provenance is taken from what was read before the call. Artifacts
written before H12.12 lack the new fields and keep their old rule, so the
upgrade regenerates nothing by itself.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
from smriti.daylog.render import head_field, read_head, render_day
from smriti.daylog.rollup import Period, build_rollup, enumerate_stale_periods
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

"""H12.12: a digest's or rollup's freshness follows everything that made it.

A rollup is stale when a member's content changes, not only the turns behind
it; provenance is taken from what was read before the call. Artifacts
written before H12.12 lack the new fields and keep their old rule, so the
upgrade regenerates nothing by itself.

H15.7a: a render or digest is stale when a turn behind it is edited in place
(its ``content_hash``), and a day whose day-file holds nothing loses both.
Artifacts written before H15.7a record no content hash and are judged by
their input hash alone.
"""

from __future__ import annotations

import json
import os
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
from smriti.daylog.render import (
    head_field,
    read_head,
    read_input_hash,
    render_day,
    render_snapshot,
)
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


def _drop_fields(path: Path, *names: str) -> None:
    """Drop frontmatter fields *names* from *path*: an artifact as an older smriti wrote it."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(ln for ln in lines if not ln.startswith(tuple(f"{n}:" for n in names))),
                    encoding="utf-8")


def _write_old_format_rollup(period: Period) -> None:
    """A rollup as smriti wrote it before H12.12: ``inputs_hash`` and nothing newer,
    over members as they were then (no ``content_hash``, which came with H15.7a)."""
    for member in period.inputs:
        _drop_fields(member, "content_hash")
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
    # Its rule sees input hashes only: a digest an older smriti rewrote from the
    # same turns doesn't make it stale, as it never did; nothing is regenerated
    # for lacking the new field. (One rewritten since H15.7a does: below.)
    llm.text = "## Highlights\n- the same day, told differently"
    digest_mod.digest_day(cfg, DAYS[0], [])
    _drop_fields(cfg.day_digest(DAYS[0]), "content_hash")
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
    """Drop the fields H12.12 added, and H15.7a's: the artifact as an older smriti wrote it."""
    _drop_fields(path, "prompt_fp", "members_hash", "content_hash")


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


# ------------------------------------------------- turns edited or removed (H15.7a)


def _edit_turn(cfg: DaylogConfig, day: str, old: str, new: str) -> None:
    """Edit a turn's text in its day-file in place, keeping its id: a correction, a redaction."""
    path = cfg.day_jsonl(day)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record["text"] == old:
            record["text"] = new
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _content(path: Path) -> str:
    return head_field(read_head(path), "content_hash")


@pytest.mark.parametrize("day", [DAY, "2026-07-01"], ids=["in-the-window", "outside-it"])
def test_a_turn_edited_in_place_makes_its_day_stale(
        tmp_path: Path, llm: FakeProvider, day: str) -> None:
    """The input hash hashes turn ids, which an edit keeps: the old render and
    digest stayed current, the old text searchable (Codex, review #8)."""
    cfg = _cfg(tmp_path)
    _seed(cfg, day)
    _night(cfg, NIGHT, rollup_cap=0)
    ids, content = read_input_hash(cfg.day_md(day)), _content(cfg.day_md(day))

    _edit_turn(cfg, day, f"question on {day}", "question on [redacted]")
    steps = _night(cfg, NIGHT.replace(minute=30), rollup_cap=0)
    assert steps["render"]["days"] == [day] and steps["digest"]["days"] == [day]
    assert content  # the first night's render and digest recorded it
    text = cfg.day_md(day).read_text(encoding="utf-8")
    assert "[redacted]" in text and f"question on {day}" not in text
    assert "[redacted]" in llm.requests[-1].user
    assert read_input_hash(cfg.day_md(day)) == ids  # the same turns
    assert _content(cfg.day_md(day)) == _content(cfg.day_digest(day)) != content
    assert _night(cfg, NIGHT.replace(minute=45), rollup_cap=0)["digest"]["days"] == []


def test_a_day_written_before_the_content_hash_is_current_while_its_turns_are(
        tmp_path: Path, llm: FakeProvider) -> None:
    """An artifact that records no content_hash is judged by its input hash, as before."""
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _night(cfg, NIGHT)
    for path in (cfg.day_md(DAY), cfg.day_digest(DAY)):
        _drop_fields(path, "content_hash")
    served = len(llm.requests)
    steps = _night(cfg, NIGHT.replace(minute=30))
    assert steps["render"]["days"] == [] and steps["digest"]["days"] == []
    assert len(llm.requests) == served
    assert _content(cfg.day_md(DAY)) == _content(cfg.day_digest(DAY)) == ""

    append_turns(cfg, [_turn(f"{DAY}T03:00:00Z", "a late turn")])  # what the old rule sees
    assert _night(cfg, NIGHT.replace(minute=45))["digest"]["days"] == [DAY]
    assert _content(cfg.day_md(DAY)) == _content(cfg.day_digest(DAY)) != ""


def test_the_upgrade_renders_and_digests_nothing_again(
        tmp_path: Path, llm: FakeProvider) -> None:
    """A tree an older smriti wrote: no content_hash anywhere, and a version-1 scan
    marker. The first night reads every day-file once and rewrites nothing."""
    cfg = _cfg(tmp_path)
    days = ("2026-07-01", "2026-07-02", "2026-09-01", DAY)
    for day in days:
        _seed(cfg, day)
    _night(cfg, NIGHT, rollup_cap=0)
    artifacts = [p for day in days for p in (cfg.day_md(day), cfg.day_digest(day))]
    for path in artifacts:
        _drop_fields(path, "content_hash")
    marker = cfg.root / ".smriti" / "nightly-scan.json"
    raw = json.loads(marker.read_text(encoding="utf-8"))
    raw["version"] = 1
    raw["days"] = {day: entry[:4] for day, entry in raw["days"].items()}
    marker.write_text(json.dumps(raw), encoding="utf-8")
    before = {path: path.read_bytes() for path in artifacts}
    served = len(llm.requests)

    steps = _night(cfg, NIGHT.replace(minute=30), rollup_cap=0)
    assert steps["render"]["days"] == [] and steps["digest"]["days"] == []
    assert steps["render"]["scan"]["full"] is True  # the version-1 marker is not trusted
    assert len(llm.requests) == served
    assert {path: path.read_bytes() for path in artifacts} == before
    assert json.loads(marker.read_text(encoding="utf-8"))["version"] == 2


def test_a_day_emptied_of_turns_loses_its_render_and_digest(
        tmp_path: Path, llm: FakeProvider) -> None:
    cfg = _cfg(tmp_path)
    for day in ("2026-07-01", DAY):  # outside the window, and in it
        _seed(cfg, day)
    _night(cfg, NIGHT)
    for day in ("2026-07-01", DAY):
        assert cfg.day_md(day).exists() and cfg.day_digest(day).exists()
        cfg.day_jsonl(day).write_text("", encoding="utf-8")  # every turn removed
    steps = _night(cfg, NIGHT.replace(minute=30))
    for day in ("2026-07-01", DAY):
        assert not cfg.day_md(day).exists() and not cfg.day_digest(day).exists()
    assert steps["render"]["ok"] is True
    assert steps["render"]["emptied"] == ["2026-07-01", DAY]
    assert "emptied" not in _night(cfg, NIGHT.replace(minute=45))["render"]


@pytest.mark.parametrize("how", ["deleted", "garbled"])
def test_a_day_file_gone_or_garbled_keeps_its_render_and_digest(
        tmp_path: Path, llm: FakeProvider, how: str) -> None:
    """Only a day-file that holds nothing says its turns were removed. A missing one
    can be a tree restored without its day-files (git-ignored), or old ones pruned
    to keep the digests; a garbled one is damage the status names."""
    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _night(cfg, NIGHT)
    if how == "deleted":
        cfg.day_jsonl(DAY).unlink()
    else:
        cfg.day_jsonl(DAY).write_text("not a turn\n", encoding="utf-8")
    steps = _night(cfg, NIGHT.replace(minute=30))
    assert cfg.day_md(DAY).exists() and cfg.day_digest(DAY).exists()
    assert "emptied" not in steps["render"]


def test_a_day_file_pruned_after_the_listing_keeps_its_render_and_digest(
        tmp_path: Path, llm: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """A day-file listed by the scan and gone by its read reads as no turns; that is
    no sign it was emptied, and pruning it must not take its digest (Codex)."""
    from smriti.daylog import nightly
    from smriti.daylog.writer import read_day

    cfg = _cfg(tmp_path)
    _seed(cfg, DAY)
    _night(cfg, NIGHT)
    path = cfg.day_jsonl(DAY)

    def pruned_after_the_listing(target: Path) -> object:
        if target == path and target.exists():
            target.unlink()
        return read_day(target)

    monkeypatch.setattr(nightly, "read_day", pruned_after_the_listing)
    steps = _night(cfg, NIGHT.replace(minute=30))
    assert not path.exists()
    assert cfg.day_md(DAY).exists() and cfg.day_digest(DAY).exists()
    assert "emptied" not in steps["render"]
    path.write_text("", encoding="utf-8")
    assert read_day(path).opened and not read_day(tmp_path / "missing.jsonl").opened


def test_a_day_the_scan_took_for_emptied_is_read_again_before_its_artifacts_go(
        tmp_path: Path, llm: FakeProvider) -> None:
    """The scan's word can be the marker's, from an older read: here the day-file
    held only blank lines then, and turns again now, at the same size and mtime."""
    from smriti.daylog import nightly

    day = "2026-07-01"  # outside the window: judged from the marker when unchanged
    cfg = _cfg(tmp_path)
    _seed(cfg, day)
    _night(cfg, NIGHT, rollup_cap=0)
    path = cfg.day_jsonl(day)
    turns = path.read_bytes()
    path.write_bytes(b"\n" * len(turns))  # no turn, and no line that isn't one
    assert nightly._scan_days(cfg, set(), set(), date(2026, 9, 4)).emptied == [day]
    seen = path.stat().st_mtime_ns
    path.write_bytes(turns)  # the turns back, at the size and mtime the marker holds
    os.utime(path, ns=(seen, seen))

    steps = _night(cfg, NIGHT.replace(minute=30), rollup_cap=0)
    assert cfg.day_md(day).exists() and cfg.day_digest(day).exists()
    assert "emptied" not in steps["render"]


def test_an_emptied_day_leaves_its_week_and_a_week_left_with_none_is_named(
        tmp_path: Path, llm: FakeProvider) -> None:
    """The week is regenerated from the digests left (and its month after it). A
    week with none left keeps its rollup: no period stands for it any more."""
    cfg = _cfg(tmp_path)
    for day in (*DAYS, "2026-09-09"):  # two days in week 1, one in week 2
        _seed(cfg, day)
    late = datetime(2026, 9, 19, 17, 5, tzinfo=timezone.utc)  # target 09-19: both weeks closed
    built = _night(cfg, late, rollup_cap=10)["rollups"]["built"]
    assert sorted(built) == ["week1.md", "week2.md"]
    month = cfg.log_dir / "2026" / "09"
    week1, week2 = month / "week1.md", month / "week2.md"
    for day in (DAYS[0], "2026-09-09"):
        cfg.day_jsonl(day).write_text("", encoding="utf-8")

    steps = _night(cfg, late.replace(minute=30), rollup_cap=10)
    assert steps["render"]["emptied"] == [DAYS[0], "2026-09-09"]
    assert steps["rollups"]["built"] == ["week1.md"]
    assert steps["rollups"]["orphaned"] == ["2026/09/week2.md"]
    request = llm.requests[-1].user
    assert "02-digest.md" in request and "01-digest.md" not in request
    assert week1.exists() and week2.exists()


def test_a_digest_rewritten_since_the_content_hash_makes_an_old_format_week_stale(
        tmp_path: Path, llm: FakeProvider) -> None:
    """The old rule sees the members' input hashes, which an edit in place keeps; a
    member that records content_hash was written after any old-format rollup."""
    cfg = _cfg(tmp_path)
    _digested(cfg, *DAYS)
    _write_old_format_rollup(_week(cfg))
    assert _stale_names(cfg) == []
    ids = read_input_hash(cfg.day_digest(DAYS[0]))
    _edit_turn(cfg, DAYS[0], f"question on {DAYS[0]}", "question on [redacted]")
    snapshot = render_snapshot(cfg, DAYS[0])
    assert snapshot is not None
    digest_mod.digest_day(cfg, DAYS[0], [], snapshot=snapshot)
    assert read_input_hash(cfg.day_digest(DAYS[0])) == ids
    assert _stale_names(cfg) == ["week1.md"]

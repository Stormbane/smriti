"""Digest coverage: what of a day's log reaches the digest, and in how many calls.

The rendered day logs here are written by hand in ``render.py``'s format, so the
speakers can be synthetic ("Sam" and "Ari"). The captured turns are one
unattended session, which gives ``digest_day`` a day to digest and an
autonomous-activity sample to pass on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, parse_ts
from smriti.daylog.writer import append_turns, read_day_turns
from smriti.llm import LLMError
from smriti.llm.types import LLMRequest, LLMResponse

DAY = "2026-09-04"
_FILLER = "the notes were updated and the build ran again; "
_MARKER = "[autonomous: claude:proj, 12:00–12:02, 3 turns]"
_SPAN_RE = re.compile(r"covering (\d\d:\d\d)–(\d\d:\d\d)\.")
_BUDGET = digest_mod._MAX_LOG_CHARS


class NotesProvider:
    """Fake LLM: call n answers '## Highlights / - note n'. Records every request."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=f"## Highlights\n- note {len(self.requests)}")


@pytest.fixture(autouse=True)
def _fixed_day_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> NotesProvider:
    provider = NotesProvider()
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: provider)
    return provider


def _turn(minute: int, text: str) -> Turn:
    ts = parse_ts(f"{DAY}T02:{minute:02d}:00Z")  # 12:0x local
    assert ts is not None
    return Turn(ts=ts, channel="claude:proj", who="agent", text=text, session="auto1")


def _rendered_day(n_turns: int, turn_chars: int) -> str:
    """A day log in render.py's format: header, timed turns, autonomous markers.

    Each turn has its own minute, so its clock names it; every third turn runs
    over two lines, indented the way render.py indents a multi-line turn.
    """
    lines = ["---", f"date: {DAY}", "input_hash: test", "generated_by: test", "---", "",
             f"# Day log — {DAY}", ""]
    for i in range(n_turns):
        clock = _clock(i, n_turns)
        speaker = "Sam" if i % 2 == 0 else "Ari"
        channel = " [voice]" if i == 0 else ""
        body = (f"turn {i:04d} " + _FILLER * (turn_chars // len(_FILLER) + 1))[:turn_chars]
        if i % 3 == 0:
            body = body[: turn_chars // 2] + "\n  " + body[turn_chars // 2:]
        lines.append(f"- `{clock}`{channel} **{speaker}:** {body}")
    lines += ["", "## Autonomous sessions", "", f"- {_MARKER}"]
    return "\n".join(lines) + "\n"


def _clock(i: int, n_turns: int) -> str:
    minute = i * (24 * 60) // n_turns
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _turn_blocks(log_text: str) -> list[str]:
    """The log's turns as render.py writes them: a turn line and its indented lines."""
    timeline = log_text[: log_text.index("## Autonomous sessions")]
    return [t.rstrip("\n") for t in re.split(r"(?m)^(?=- `)", timeline)[1:]]


def _turn_numbers(requests: list[LLMRequest]) -> list[int]:
    return [int(n) for r in requests for n in re.findall(r"turn (\d{4})", r.user)]


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return {key: value for key, _, value in (line.partition(": ") for line in lines[1:end])}


def _sized(log_text: str, size: int) -> str:
    """*log_text* padded inside its first turn to exactly *size* characters."""
    at = log_text.index("\n", log_text.index("- `"))
    padded = log_text[:at] + "x" * (size - len(log_text)) + log_text[at:]
    assert len(padded) == size
    return padded


def _seed(tmp_path: Path, log_text: str) -> DaylogConfig:
    cfg = DaylogConfig(root=tmp_path, sources=[])
    append_turns(cfg, [_turn(0, "loop started"), _turn(1, "checked the queue"),
                       _turn(2, "loop finished")])
    path = cfg.day_md(DAY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(log_text, encoding="utf-8")
    return cfg


def _sample(cfg: DaylogConfig) -> str:
    sample = digest_mod._autonomous_sample(read_day_turns(cfg.day_jsonl(DAY)))
    assert sample  # the fixture day has unattended activity to report
    return sample


def test_short_day_is_one_call_with_the_whole_log(tmp_path: Path, llm: NotesProvider) -> None:
    log_text = _rendered_day(n_turns=5, turn_chars=200)
    cfg = _seed(tmp_path, log_text)
    out = digest_mod.digest_day(cfg, DAY, [])
    assert out is not None
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.system == digest_mod._SYSTEM
    assert request.user == (f"Day log for {DAY}:\n\n{log_text}"
                            f"\n\nAutonomous activity sample:\n{_sample(cfg)}")
    assert "note 1" in out.read_text(encoding="utf-8")
    meta = _frontmatter(out)
    assert meta["parts"] == "1" and meta["cut"] == "false"
    assert meta["chars_seen"] == meta["chars_total"] == str(len(log_text))
    assert "cut_from" not in meta


def test_one_call_up_to_the_budget_and_parts_past_it(tmp_path: Path, llm: NotesProvider) -> None:
    base = _rendered_day(n_turns=40, turn_chars=1000)
    cfg = _seed(tmp_path, _sized(base, _BUDGET))
    digest_mod.digest_day(cfg, DAY, [])
    assert [r.system for r in llm.requests] == [digest_mod._SYSTEM]

    llm.requests.clear()
    cfg.day_md(DAY).write_text(_sized(base, _BUDGET + 1), encoding="utf-8")
    digest_mod.digest_day(cfg, DAY, [])
    *part_calls, merge = llm.requests
    assert part_calls and {r.system for r in part_calls} == {digest_mod._PART_SYSTEM}
    assert merge.system == digest_mod._MERGE_SYSTEM


def test_a_200k_day_is_four_parts_in_time_order_and_one_merge(
    tmp_path: Path, llm: NotesProvider
) -> None:
    log_text = _rendered_day(n_turns=200, turn_chars=975)
    assert 190_000 < len(log_text) < 4 * _BUDGET
    cfg = _seed(tmp_path, log_text)
    out = digest_mod.digest_day(cfg, DAY, [])
    assert out is not None

    assert len(llm.requests) == 5
    *part_calls, merge = llm.requests
    assert [r.system for r in part_calls] == [digest_mod._PART_SYSTEM] * 4
    assert merge.system == digest_mod._MERGE_SYSTEM

    # In time order, and nothing lost or repeated: turns 0..199 run across the parts.
    assert _turn_numbers(part_calls) == list(range(200))
    # Each turn lands whole in exactly one part, and no part is over budget.
    for turn in _turn_blocks(log_text):
        assert sum(turn in r.user for r in part_calls) == 1
    clocks: list[str] = []
    for i, request in enumerate(part_calls, start=1):
        assert request.user.startswith(f"Day log for {DAY}, part {i} of 4, covering ")
        assert len(request.user.split("\n\n", 1)[1]) <= _BUDGET
        span = _SPAN_RE.search(request.user)
        assert span is not None
        clocks += span.groups()
    assert clocks[0] == _clock(0, 200) and clocks[-1] == _clock(199, 200)
    assert clocks == sorted(clocks) and len(set(clocks)) == len(clocks)

    # The merge gets every part's notes in order, the markers, and the sample once.
    at = [merge.user.index(f"- note {i}\n") for i in range(1, 5)]
    assert at == sorted(at)
    sample = _sample(cfg)
    assert merge.user.count(sample) == 1
    assert not any(sample in r.user for r in part_calls)
    assert _MARKER in merge.user

    # Nothing was cut, and every prompt is told so.
    whole = f"The digest sees the whole day log ({len(log_text):,} characters)."
    for request in llm.requests:
        assert whole in request.user and "was cut" not in request.user
    meta = _frontmatter(out)
    assert meta["parts"] == "4" and meta["cut"] == "false"
    assert meta["chars_seen"] == meta["chars_total"] == str(len(log_text))
    assert "cut_from" not in meta
    assert out.read_text(encoding="utf-8").rstrip().endswith("- note 5")  # the merge's text


def test_a_day_past_four_parts_records_the_cut(tmp_path: Path, llm: NotesProvider) -> None:
    log_text = _rendered_day(n_turns=300, turn_chars=975)
    total = len(log_text)
    assert total > 4 * _BUDGET
    cfg = _seed(tmp_path, log_text)
    out = digest_mod.digest_day(cfg, DAY, [])
    assert out is not None

    assert len(llm.requests) == 5  # still four parts and one merge
    *part_calls, merge = llm.requests
    seen = _turn_numbers(part_calls)
    first_cut = len(seen)
    assert seen == list(range(first_cut)) and first_cut < 300  # the evening is what's cut

    cut_from = _clock(first_cut, 300)
    meta = _frontmatter(out)
    assert meta["parts"] == "4" and meta["cut"] == "true"
    assert meta["cut_from"] == f'"{cut_from}"'
    assert meta["chars_total"] == str(total)
    # Exactly the turns from the cut on are unseen; the autonomous markers still reach the merge.
    unseen = log_text.index("## Autonomous sessions") - log_text.index(f"- `{cut_from}`")
    assert meta["chars_seen"] == str(total - unseen)
    assert _MARKER in merge.user

    # Every prompt, each part's and the merge's, is told how much it sees and where the cut is.
    note = (f"The digest sees {total - unseen:,} of the day log's {total:,} characters: "
            f"the log was cut at {cut_from}, and nothing after that point reaches the digest.")
    for request in llm.requests:
        assert note in request.user


def test_only_a_turn_larger_than_a_part_is_split() -> None:
    before = "- `08:00` **Sam:** before\n"
    after = "- `10:00` **Sam:** after\n"
    giant = "- `09:00` **Ari:** " + "y" * (2 * _BUDGET + 10_000) + "\n"
    log_text = f"---\ndate: {DAY}\n---\n\n# Day log — {DAY}\n\n" + before + giant + after
    plan = digest_mod._plan(log_text)
    assert not plan.coverage.cut and plan.coverage.parts == len(plan.parts) == 4
    assert "".join(p.text for p in plan.parts) + plan.trailer == log_text
    assert all(len(p.text) <= _BUDGET for p in plan.parts)
    assert before in plan.parts[0].text and after in plan.parts[3].text  # small turns whole
    assert [p.span() for p in plan.parts] == ["08:00–08:00", "09:00–09:00", "09:00–09:00",
                                              "09:00–10:00"]


def test_a_heading_inside_the_timeline_is_not_the_trailer() -> None:
    # A lone CR in a turn's text reads back as a newline, so a turn can
    # contain an unindented line; only a heading after the last turn
    # starts the sections that go to the merge.
    log_text = ("# Day log\n\n- `08:00` **Sam:** a list\n## not a section\n"
                "- `09:00` **Ari:** reply\n\n## Autonomous sessions\n\n- [autonomous: x]\n")
    plan = digest_mod._plan(log_text)
    assert plan.trailer == "## Autonomous sessions\n\n- [autonomous: x]\n"
    assert plan.parts[0].text.endswith("- `09:00` **Ari:** reply\n\n")
    assert plan.parts[0].span() == "08:00–09:00"


def test_a_failed_part_call_writes_no_digest(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    class FailsAfterTwo(NotesProvider):
        def call(self, request: LLMRequest) -> LLMResponse:
            if len(self.requests) >= 2:
                raise RuntimeError("seat down")
            return super().call(request)

    provider = FailsAfterTwo()
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: provider)
    cfg = _seed(tmp_path, _rendered_day(n_turns=200, turn_chars=975))
    with pytest.raises(LLMError):
        digest_mod.digest_day(cfg, DAY, [])
    assert not cfg.day_digest(DAY).exists()  # no digest from half a day's notes

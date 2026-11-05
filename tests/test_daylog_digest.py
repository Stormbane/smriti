"""Digest coverage: what of a day's log reaches the digest, and in how many calls.

The rendered day logs here are written by hand in ``render.py``'s format, so the
speakers can be synthetic ("Sam" and "Ari"). The captured turns are one
unattended session, which gives ``digest_day`` a day to digest and an
autonomous-activity sample to pass on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, parse_ts
from smriti.daylog.writer import append_turns, read_day_turns
from smriti.llm.types import LLMRequest, LLMResponse

DAY = "2026-09-04"
_FILLER = "the notes were updated and the build ran again; "


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
        minute = i * (24 * 60) // n_turns
        clock = f"{minute // 60:02d}:{minute % 60:02d}"
        speaker = "Sam" if i % 2 == 0 else "Ari"
        channel = " [voice]" if i == 0 else ""
        body = (f"turn {i:04d} " + _FILLER * (turn_chars // len(_FILLER) + 1))[:turn_chars]
        if i % 3 == 0:
            body = body[: turn_chars // 2] + "\n  " + body[turn_chars // 2:]
        lines.append(f"- `{clock}`{channel} **{speaker}:** {body}")
    lines += ["", "## Autonomous sessions", "",
              "- [autonomous: claude:proj, 12:00–12:02, 3 turns]"]
    return "\n".join(lines) + "\n"


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

"""H12.4: a canned LLM that CI's smoke run can select, and nothing else can.

The wheel smoke job runs one nightly with no real LLM. ``test_stub`` gives
canned answers, but only where ``SMRITI_ALLOW_TEST_STUB=1`` says the process
is a test: elsewhere selecting it fails the night instead of writing canned
digests into a memory tree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
from smriti.daylog.nightly import run_nightly
from smriti.daylog.writer import append_turns
from smriti.llm import factory
from smriti.llm.providers import stub as test_stub
from smriti.llm.types import LLMError, LLMRequest


@pytest.fixture(autouse=True)
def fresh_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "_CACHE", {})
    monkeypatch.delenv(test_stub.ALLOW_ENV, raising=False)


def test_refused_outside_a_test() -> None:
    with pytest.raises(LLMError, match="SMRITI_ALLOW_TEST_STUB=1"):
        factory.get_provider("test_stub")
    assert "test_stub" not in factory._CACHE


def test_never_listed_or_auto_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(test_stub.ALLOW_ENV, "1")
    monkeypatch.delenv("SMRITI_LLM_PROVIDER", raising=False)
    assert "test_stub" not in {row["name"] for row in factory.list_providers()}
    assert factory._autodetect() != "test_stub"


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_exactly_one_allows_it(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(test_stub.ALLOW_ENV, value)
    with pytest.raises(LLMError):
        factory.get_provider("test_stub")


def test_answers_in_a_test_and_stops_when_the_test_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(test_stub.ALLOW_ENV, "1")
    provider = factory.get_provider("test_stub")
    assert provider.is_available()
    reply = provider.call(LLMRequest(system="digest", user="Day log for 2026-05-14\nSam: hi"))
    assert reply.text.startswith("## Highlights\n- test_stub: a canned answer")
    assert "Day log for 2026-05-14" in reply.text

    monkeypatch.delenv(test_stub.ALLOW_ENV)
    assert not provider.is_available()
    with pytest.raises(LLMError):
        provider.call(LLMRequest(system="digest", user="again"))


def _seed(cfg: DaylogConfig) -> None:
    def turn(ts: str, who: str, text: str) -> Turn:
        parsed = parse_ts(ts)
        assert parsed is not None
        return Turn(ts=parsed, channel="voice", who=who, text=text, session="s1")

    append_turns(cfg, [turn("2026-05-14T01:00:00Z", USER, "Which glue for the hinge?"),
                       turn("2026-05-14T01:00:05Z", AGENT, "Wheat starch paste.")])


@pytest.mark.parametrize("allowed", [True, False])
def test_the_nightly_digests_with_it_only_in_a_test(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allowed: bool) -> None:
    monkeypatch.setenv("SMRITI_NIGHTLY_PROVIDER", "test_stub")
    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", "")
    if allowed:
        monkeypatch.setenv(test_stub.ALLOW_ENV, "1")
    cfg = DaylogConfig(root=tmp_path, sources=[], user_name="Sam", agent_name="Ari")
    _seed(cfg)

    status = run_nightly(cfg, now=datetime(2026, 5, 15, 3, 5, tzinfo=timezone.utc),
                         reindex=False, witness=False)

    digest = cfg.day_digest("2026-05-14")
    steps = status["steps"]
    assert isinstance(steps, dict)
    if allowed:
        assert steps["digest"]["ok"] and steps["digest"]["days"] == ["2026-05-14"]
        assert "test_stub: a canned answer" in digest.read_text(encoding="utf-8")
    else:
        assert not steps["digest"]["ok"] and not status["ok"]
        assert "SMRITI_ALLOW_TEST_STUB=1" in steps["digest"]["errors"]["2026-05-14"]
        assert not digest.exists()

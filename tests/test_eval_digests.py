"""H12.9: digest coverage and factuality, the measures behind `smriti eval --llm`.

The measures run on any digest; `--llm` makes the digests with a real model,
locally. Here a fake model stands in, so the plumbing and the arithmetic are
held without one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.daylog import digest as digest_mod
from smriti.eval.digests import DIGEST_DAYS, run_digest_quality, score_digest, terms
from smriti.llm.types import LLMError, LLMRequest, LLMResponse

WORKSHOP = DIGEST_DAYS[0]
FAITHFUL = """---
date: 2026-05-15
---
## Highlights
- The cloth hinges stay in the press under weight until tomorrow.
- Ari drafted the sign-up sheet for Saturday: ten places and a waitlist of three.
- Everyone brings one damaged paperback; the larger room opens at nine.
- Ari will order two spools of linen thread for the blue cabinet.
"""


def test_terms() -> None:
    assert terms("The hinges, Saturday's class!") == {"hinge", "saturd", "clas"}
    assert terms("hinge Saturday") <= terms("The hinges, Saturday's class!")
    assert terms("it is on to be") == set()


def test_a_faithful_digest_scores_high() -> None:
    score = score_digest([t for _, t in WORKSHOP.turns], FAITHFUL, WORKSHOP.facts)
    assert score.facts == 1.0 and not score.missed_facts
    assert score.traceable == 1.0 and not score.untraceable
    assert score.coverage >= 0.75
    assert score.sentences == 4 and score.turns == 8


def test_an_invention_and_an_omission_are_named() -> None:
    digest = FAITHFUL.replace("- Ari will order two spools of linen thread for the blue cabinet.\n",
                              "- Sam adopted a tabby kitten called Biscuit yesterday.\n")
    score = score_digest([t for _, t in WORKSHOP.turns], digest, WORKSHOP.facts)
    assert score.missed_facts == ("linen thread ordered",)
    assert score.untraceable == ("Sam adopted a tabby kitten called Biscuit yesterday.",)
    assert score.traceable == 3 / 4


def test_an_empty_digest_covers_nothing() -> None:
    score = score_digest([t for _, t in WORKSHOP.turns], "## Highlights\n", WORKSHOP.facts)
    assert score.coverage == 0.0 and score.facts == 0.0 and score.sentences == 0


class EchoModel:
    """Answers with the day's own turns, a line each: every measure at its best."""

    def is_available(self) -> bool:
        return True

    def call(self, request: LLMRequest) -> LLMResponse:
        lines = [line.split(":** ", 1)[1] for line in request.user.splitlines()
                 if ":** " in line]
        return LLMResponse(text="## Highlights\n" + "\n".join(f"- {line}" for line in lines))


def test_each_synthetic_day_is_digested_and_scored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: EchoModel())
    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", "")

    report = run_digest_quality(tmp_path)

    assert not report.errors
    assert set(report.days) == {day.id for day in DIGEST_DAYS}
    for score in report.days.values():
        assert score.coverage == 1.0 and score.traceable == 1.0 and score.facts == 1.0
    assert report.means() == {"coverage": 1.0, "facts": 1.0, "traceable": 1.0}
    assert report.providers and all(a["outcome"] == "ok" for a in report.providers)


def test_a_failed_call_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(EchoModel):
        def call(self, request: LLMRequest) -> LLMResponse:
            raise LLMError("no seat today")

    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: Failing())
    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", "")

    report = run_digest_quality(tmp_path)

    assert not report.days
    assert all("no seat today" in error for error in report.errors.values())
    assert report.as_dict()["means"] == {}


def test_smriti_eval_llm(monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main
    from tests.test_eval_sandbox import LocalEmbedding

    monkeypatch.setattr("smriti.embed.provider.embedder", lambda: LocalEmbedding())
    monkeypatch.setattr(digest_mod, "get_provider", lambda name=None: EchoModel())
    monkeypatch.setenv("SMRITI_NIGHTLY_FALLBACK", "")

    assert main(["eval", "--llm"]) == 0
    out = capsys.readouterr().out
    assert "DIGESTS" in out and "mean: coverage 100%, facts 100%, traceable 100%" in out

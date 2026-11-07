"""Digest coverage and factuality, measured on synthetic days with a real LLM.

``smriti eval --llm`` writes each synthetic day below into a disposable
day-log, digests it as the nightly does (the same prompts, through the
nightly's provider chain: ``SMRITI_NIGHTLY_PROVIDER``, ``claude_cli`` by
default), and measures the digest against the day's turns. It calls a
real model, so it runs locally, never in CI, and costs one call per day.

The measures are lexical proxies, cheap and repeatable, not judgements:

- ``coverage``: the share of the day's substantive turns (two content terms
  or more) the digest reflects, a turn counting when at least
  :data:`COVER_MIN` of its content terms appear in the digest;
- ``facts``: the share of the day's listed facts the digest states, each
  fact a set of terms that must all appear (any spelling of each);
- ``traceable``: the share of the digest's sentences (two content terms
  or more) whose terms are at least :data:`TRACE_MIN` the day's own. A
  sentence below it says something the turns don't: a paraphrase the
  proxy misses, or an invention.

Content terms are words of four letters or more outside a short stopword
list, with an -ing, -ed or -s ending taken off and cut to six letters, so
"hinges" meets "hinge" and "Saturday's" meets "Saturday" (a crude stem: a
paraphrase in other words still counts as untraceable). All text is
invented (the agent Ari, the user Sam).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COVER_MIN = 0.34
TRACE_MIN = 0.5

_STOP = frozenset("""
about above after again also been before being both could does doing down each
from have having here into just more most much need needs only other over same
should some such than that their them then there these they this those through
today tomorrow very want were what when where which while will with would your
yours with without morning
""".split())

_WORD = re.compile(r"[a-z]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def terms(text: str) -> set[str]:
    """The content terms of *text* (see the module docstring)."""
    out: set[str] = set()
    for word in _WORD.findall(text.lower().replace("'", "").replace("’", "")):
        if len(word) < 4 or word in _STOP:
            continue
        for suffix in ("ing", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        out.add(word[:6])
    return out


def digest_sentences(digest: str) -> list[str]:
    """The digest's sentences: frontmatter, headings and list markers left out."""
    body = digest
    if body.startswith("---"):
        end = body.find("\n---", 3)
        body = body[end + 4:] if end != -1 else body
    lines = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line) for line in body.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    return [s.strip() for s in _SENTENCE.split("\n".join(lines)) if s.strip()]


@dataclass(frozen=True)
class DigestScore:
    coverage: float  # share of substantive turns the digest reflects
    facts: float  # share of listed facts it states
    traceable: float  # share of its sentences mostly made of the day's own terms
    turns: int
    sentences: int
    missed_facts: tuple[str, ...] = ()
    untraceable: tuple[str, ...] = ()


def score_digest(turn_texts: list[str], digest: str,
                 facts: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = ()) -> DigestScore:
    """Score *digest* against the turns it summarises (and the day's *facts*)."""
    digest_terms = terms(digest)
    substantive = [t for t in map(terms, turn_texts) if len(t) >= 2]
    covered = sum(1 for t in substantive if len(t & digest_terms) / len(t) >= COVER_MIN)
    day_terms: set[str] = set().union(*map(terms, turn_texts)) if turn_texts else set()
    sentences = [(s, terms(s)) for s in digest_sentences(digest)]
    sentences = [(s, t) for s, t in sentences if len(t) >= 2]
    untraceable = tuple(s for s, t in sentences if len(t & day_terms) / len(t) < TRACE_MIN)
    lowered = digest.lower()
    missed = tuple(name for name, groups in facts
                   if not all(any(alt in lowered for alt in group) for group in groups))
    return DigestScore(
        coverage=covered / len(substantive) if substantive else 1.0,
        facts=(len(facts) - len(missed)) / len(facts) if facts else 1.0,
        traceable=(len(sentences) - len(untraceable)) / len(sentences) if sentences else 1.0,
        turns=len(substantive), sentences=len(sentences),
        missed_facts=missed, untraceable=untraceable,
    )


@dataclass(frozen=True)
class SyntheticDay:
    id: str
    turns: tuple[tuple[str, str], ...]  # (speaker role, text), a minute apart
    facts: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]  # (name, term groups)


DIGEST_DAYS: tuple[SyntheticDay, ...] = (
    SyntheticDay("d-workshop", (
        ("user", "Morning Ari. The cloth hinges from Saturday's class are still tacky."),
        ("agent", "Methyl cellulose needs a full night under weight. Leave the books in the "
                  "press until tomorrow."),
        ("user", "Fine. Can you draft the sign-up sheet for next Saturday? Ten places."),
        ("agent", "Drafted: ten places, first come first served, and a waitlist of three."),
        ("user", "Add a line asking everyone to bring one damaged paperback."),
        ("agent", "Added. I also noted that the larger room opens at nine."),
        ("user", "Order more linen thread too, the blue cabinet is nearly empty."),
        ("agent", "I'll order two spools of linen thread today."),
    ), (
        ("hinges stay in the press", (("press",), ("hinge", "book"))),
        ("ten places", (("ten", "10"),)),
        ("a waitlist", (("waitlist", "wait list", "waiting list"),)),
        ("bring a paperback", (("paperback",),)),
        ("linen thread ordered", (("linen",), ("thread",))),
    )),
    SyntheticDay("d-field-test", (
        ("user", "Kite survived the field test, but the yellow case scuffed on the rocks."),
        ("agent", "The scuffs are cosmetic. Did the AAA cells last the whole afternoon?"),
        ("user", "They did, about five hours."),
        ("agent", "Then we keep AAA cells. I'll log five hours as the measured runtime."),
        ("user", "The lid rattles. Can we fix that without glue?"),
        ("agent", "A thin felt pad under the lid would stop the rattle, with no adhesive."),
        ("user", "Do that. And remind me to call the venue about the Tuesday Lantern release."),
        ("agent", "Reminder set for Monday morning: call the venue about Tuesday's release."),
    ), (
        ("five hours on AAA", (("five", "5"), ("hour",))),
        ("AAA cells kept", (("aaa",),)),
        ("a felt pad for the rattle", (("felt",),)),
        ("call the venue", (("venue",),)),
        ("the release is Tuesday", (("tuesday",),)),
    )),
)


@dataclass
class DigestReport:
    days: dict[str, DigestScore] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    providers: list[dict[str, str]] = field(default_factory=list)

    def means(self) -> dict[str, float]:
        scores = list(self.days.values())
        if not scores:
            return {}
        return {name: sum(getattr(s, name) for s in scores) / len(scores)
                for name in ("coverage", "facts", "traceable")}

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": {k: {"coverage": round(v.coverage, 4), "facts": round(v.facts, 4),
                         "traceable": round(v.traceable, 4), "turns": v.turns,
                         "sentences": v.sentences, "missed_facts": list(v.missed_facts),
                         "untraceable": list(v.untraceable)}
                     for k, v in self.days.items()},
            "means": {k: round(v, 4) for k, v in self.means().items()},
            "errors": self.errors,
            "providers": self.providers,
        }


def run_digest_quality(root: Path, days: tuple[SyntheticDay, ...] = DIGEST_DAYS) -> DigestReport:
    """Digest each synthetic day in the disposable tree at *root* and score it.

    Each day goes on its own date, its turns a minute apart from 06:00 UTC, a
    time on the same local date in every time zone.
    """
    from smriti.daylog.config import DaylogConfig
    from smriti.daylog.digest import digest_day
    from smriti.daylog.model import AGENT, USER, Turn, day_tz
    from smriti.daylog.render import render_snapshot
    from smriti.daylog.writer import append_turns

    cfg = DaylogConfig(root=root, sources=[], user_name="Sam", agent_name="Ari")
    report = DigestReport()
    start = datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc)
    for n, day in enumerate(days):
        first = start + timedelta(days=n)
        turns = [Turn(ts=first + timedelta(minutes=i), channel="voice",
                      who=USER if who == "user" else AGENT, text=text, session=day.id)
                 for i, (who, text) in enumerate(day.turns)]
        append_turns(cfg, turns)
        key = first.astimezone(day_tz(root)).date().isoformat()
        snapshot = render_snapshot(cfg, key)
        if snapshot is None:
            report.errors[day.id] = f"nothing rendered for {key}"
            continue
        try:
            path = digest_day(cfg, key, report.providers, snapshot=snapshot)
        except Exception as exc:  # noqa: BLE001 — a failed call is reported per day
            report.errors[day.id] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        if path is None:
            report.errors[day.id] = "no digest written"
            continue
        report.days[day.id] = score_digest([text for _, text in day.turns],
                                           path.read_text(encoding="utf-8"), day.facts)
    return report

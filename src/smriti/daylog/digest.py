"""The daily digest — one read-compose-emit LLM call per day, a few for a long day.

Primary seat: ``claude_cli`` (Suti's ruling 2026-09-04); fallback:
``codex_cli``. The subprocess is constrained to no tools/MCP — it holds
no ``smriti_write`` license; anything notable belongs in the digest
file itself, which lives in the tree and gets indexed (spec: review
round 1, finding 6).

A day whose rendered log fits in one call (``_MAX_LOG_CHARS``) gets one
call. A longer day is split at turn boundaries into time-ordered parts
of at most ``_MAX_LOG_CHARS`` each, at most ``_MAX_PARTS`` of them; one
call per part writes notes, and a merge call writes the digest from the
notes. What lies beyond the last part is cut, and so are autonomous-
session markers past ``_MAX_TRAILER_CHARS``. A cut is never silent:
every prompt is told how much of the day it sees and what was cut, and
the digest's frontmatter records ``parts``, ``chars_seen``,
``chars_total`` and ``cut``, plus ``cut_from`` when turns were cut and
``markers_seen``/``markers_total`` when markers were. ``notes_cut``
counts the parts whose notes were cut to fit the merge call.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, day_tz
from smriti.daylog.render import input_hash
from smriti.daylog.writer import read_day_turns
from smriti.llm import LLMError, LLMRequest, get_provider

log = logging.getLogger(__name__)

# Context budget for one compose call. A rendered day log of at most
# _MAX_LOG_CHARS goes whole into one call; a longer one goes in parts of
# at most _MAX_LOG_CHARS, at most _MAX_PARTS of them, and the rest is cut
# (and recorded as cut).
_MAX_LOG_CHARS = 60_000
_MAX_PARTS = 4
_MAX_AUTON_CHARS = 8_000
# The merge call of a long day reads the log's trailing sections (the
# autonomous-session markers) and every part's notes, each with its own
# cap, so its input stays within one call's _MAX_LOG_CHARS (plus the
# autonomous sample). The notes need a cap of their own because the
# nightly's CLI seats (claude_cli, codex_cli) don't pass max_tokens on:
# nothing else bounds what a part call writes. A day that fits in one
# call needs neither cap, since its trailer is inside its _MAX_LOG_CHARS.
_MAX_TRAILER_CHARS = 8_000
_MAX_NOTES_CHARS = (_MAX_LOG_CHARS - _MAX_TRAILER_CHARS) // _MAX_PARTS  # 13,000

# The digest's sections, shared by the one-call and the merge prompts.
_SECTIONS = (
    "Write a compact digest in markdown:\n"
    "- '## Highlights' — the 3-8 moments that mattered, with times;\n"
    "- '## Decisions' — anything decided or ruled, verbatim where short;\n"
    "- '## Threads' — open questions and things waiting on someone;\n"
    "- '## Autonomous' — one short paragraph on unattended activity, if any.\n"
    "Be specific and faithful to the log; omit empty sections; no preamble."
)

_SYSTEM = (
    "You are Narada's nightly digest writer. You receive one day's complete "
    "communication log between Suti and Narada across every channel (voice, "
    "Telegram, Claude Code sessions, Codex), plus a sample of autonomous "
    "session activity. " + _SECTIONS
)

_PART_SYSTEM = (
    "You are the first pass of a nightly digest writer. One day's communication "
    "log across every channel is too long for one call, so it is split into "
    "time-ordered parts, and you receive one of them. Write compact notes on this "
    "part only; a later call merges every part's notes into the day's digest. "
    "Use these sections:\n"
    "- '## Highlights' — the moments that mattered, with times;\n"
    "- '## Decisions' — anything decided or ruled, verbatim where short;\n"
    "- '## Threads' — open questions and things waiting on someone, and any "
    "question this part settles.\n"
    "Be specific and faithful to the log; omit empty sections; no preamble."
)

_MERGE_SYSTEM = (
    "You are a nightly digest writer. One day's communication log across every "
    "channel was too long for one call, so it was split into time-ordered parts "
    "and each part was summarised into notes. You receive those notes in time "
    "order, plus the day's autonomous sessions and a sample of their activity, "
    "if there were any. Merge them into one digest of the whole day: keep what "
    "mattered most across the day, and treat a thread that one part opens and a "
    "later part settles as settled. If you are told part of the log was cut, "
    "end with one line saying what the digest does not cover. " + _SECTIONS
)

# A turn in the rendered log starts with "- `HH:MM`" at column 0; its
# continuation lines are indented. A "## " heading at column 0 starts
# the sections after the timeline (the autonomous-session markers).
_TURN_RE = re.compile(r"- `(\d\d:\d\d)`")
_SECTION_PREFIX = "## "

# No-tools constraint for the compose subprocess. --strict-mcp-config with
# no --mcp-config drops MCP servers entirely.
_NO_TOOLS_ARGS = ["--strict-mcp-config", "--disallowedTools", "*"]


def _providers() -> list[str]:
    primary = os.environ.get("SMRITI_NIGHTLY_PROVIDER", "claude_cli")
    fallback = os.environ.get("SMRITI_NIGHTLY_FALLBACK", "codex_cli")
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def compose(
    system: str,
    user: str,
    attempts_log: list[dict[str, str]],
    *,
    claude_cli_args: list[str] | None = None,
) -> str:
    """Run one read-compose-emit call through the provider chain.

    ``claude_cli_args`` overrides the default no-tools constraint for
    the claude_cli seat (the morning call allows web search).
    """
    last_error: Exception | None = None
    for name in _providers():
        try:
            provider = get_provider(name)
            if not provider.is_available():
                attempts_log.append({"provider": name, "outcome": "unavailable"})
                continue
            cli_args = (claude_cli_args or _NO_TOOLS_ARGS) if name == "claude_cli" else None
            response = provider.call(
                LLMRequest(system=system, user=user, max_tokens=4096, cli_args=cli_args)
            )
            attempts_log.append({"provider": name, "outcome": "ok"})
            return response.text.strip()
        except Exception as exc:  # noqa: BLE001 — record and try the fallback
            attempts_log.append({"provider": name, "outcome": f"error: {exc}"[:200]})
            last_error = exc
            log.warning("digest provider %s failed: %s", name, exc)
    raise LLMError(f"all providers failed: {last_error}")


def _autonomous_sample(turns: list[Turn]) -> str:
    by_session: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        by_session.setdefault((t.channel, t.session), []).append(t)
    parts: list[str] = []
    for (channel, _session), group in sorted(by_session.items(), key=lambda kv: kv[1][0].ts):
        if any(t.who == "suti" for t in group):
            continue
        sample = group[:3] + (group[-2:] if len(group) > 5 else [])
        parts.append(f"[{channel}] " + " / ".join(t.text[:200] for t in sample))
    return "\n".join(parts)[:_MAX_AUTON_CHARS]


@dataclass(frozen=True)
class _Coverage:
    """How much of the rendered day log reached the digest (its frontmatter)."""

    parts: int
    chars_seen: int
    chars_total: int
    timeline_cut: bool = False  # turns past the last part were left out
    cut_from: str = ""  # HH:MM of the first turn left out ("" if none, or unknown)
    trailer_cut: bool = False  # the end of the trailing sections was left out
    markers_seen: int = 0  # autonomous-session markers that reached the merge call
    markers_total: int = 0

    @property
    def cut(self) -> bool:
        return self.chars_seen < self.chars_total

    def note(self) -> str:
        """The prompts' line on how much of the day they see, and what was cut."""
        if not self.cut:
            return f"The digest sees the whole day log ({self.chars_total:,} characters)."
        reasons: list[str] = []
        if self.timeline_cut:
            at = f" at {self.cut_from}" if self.cut_from else ""
            reasons.append(f"the log was cut{at}, and nothing after that point reaches the digest")
        if self.markers_seen < self.markers_total:
            reasons.append(f"only the first {self.markers_seen:,} of its "
                           f"{self.markers_total:,} autonomous-session markers are included")
        elif self.trailer_cut:
            reasons.append("the end of the sections after its timeline is left out")
        return (f"The digest sees {self.chars_seen:,} of the day log's "
                f"{self.chars_total:,} characters: " + "; ".join(reasons) + ".")


@dataclass(frozen=True)
class _Block:
    """Consecutive lines of the rendered log: the header, a turn, or a piece of a long turn."""

    text: str
    clock: str  # HH:MM of the turn; "" for the header


@dataclass(frozen=True)
class _Part:
    """One part call's share of the log, in time order."""

    text: str
    start: str  # HH:MM of its first turn ("" if it has none)
    end: str  # HH:MM of its last turn

    def span(self) -> str:
        return f"{self.start}–{self.end}" if self.start else "no timed turns"


@dataclass(frozen=True)
class _Plan:
    """A long day divided into parts; ``trailer`` goes to the merge call."""

    parts: tuple[_Part, ...]
    trailer: str
    coverage: _Coverage


def _lines(text: str) -> list[str]:
    """*text*'s lines with their newlines kept; splits on newlines only."""
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return [line for line in lines if line]


def _whole_lines(text: str, budget: int) -> str:
    """The longest run of *text*'s first whole lines that fits in *budget* characters."""
    kept: list[str] = []
    size = 0
    for line in _lines(text):
        if size + len(line) > budget:
            break
        kept.append(line)
        size += len(line)
    return "".join(kept)


def _is_marker(line: str) -> bool:
    return line.startswith("- ")


def _pieces(text: str, clock: str) -> list[_Block]:
    """One turn as blocks of at most a part's size: whole unless it alone is larger."""
    return [_Block(text[i:i + _MAX_LOG_CHARS], clock)
            for i in range(0, len(text), _MAX_LOG_CHARS)]


def _plan(log_text: str) -> _Plan:
    """Divide a rendered day log into at most ``_MAX_PARTS`` time-ordered parts.

    The log is a header, one block per turn (render.py sorts turns by
    time), then trailing sections. Parts are filled with whole turns in
    order, and only a turn larger than a part is split. What doesn't fit
    in the parts is cut, from the first turn left out. The trailing
    sections (the autonomous-session markers) go to the merge call
    instead of a part, so a timeline cut doesn't drop them; they keep
    whole lines up to ``_MAX_TRAILER_CHARS``, and the rest is cut.
    """
    lines = _lines(log_text)
    # The trailer starts at the first heading after the last turn, so a
    # stray heading-like line inside a turn can't pull turns out of order.
    last_turn = max((i for i, line in enumerate(lines) if _TURN_RE.match(line)), default=-1)
    tail = next((i for i in range(last_turn + 1, len(lines))
                 if lines[i].startswith(_SECTION_PREFIX)), len(lines))
    full_trailer = "".join(lines[tail:])
    trailer = _whole_lines(full_trailer, _MAX_TRAILER_CHARS)
    by_turn: list[tuple[str, list[str]]] = []  # (clock, lines); the header comes first
    for line in lines[:tail]:
        match = _TURN_RE.match(line)
        if match or not by_turn:
            by_turn.append((match.group(1) if match else "", [line]))
        else:
            by_turn[-1][1].append(line)  # a continuation line, or the blank before a section

    groups: list[list[_Block]] = []
    size = 0
    timeline_cut = False
    cut_from = ""
    blocks = [block for clock, group in by_turn for block in _pieces("".join(group), clock)]
    for index, block in enumerate(blocks):
        if groups and size + len(block.text) <= _MAX_LOG_CHARS:
            groups[-1].append(block)
            size += len(block.text)
        elif len(groups) < _MAX_PARTS:
            groups.append([block])
            size = len(block.text)
        else:
            timeline_cut = True
            cut_from = next((b.clock for b in blocks[index:] if b.clock), "")
            break

    parts: list[_Part] = []
    for group in groups:
        clocks = [b.clock for b in group if b.clock]
        parts.append(_Part("".join(b.text for b in group),
                           clocks[0] if clocks else "", clocks[-1] if clocks else ""))
    seen = sum(len(p.text) for p in parts) + len(trailer)
    return _Plan(tuple(parts), trailer, _Coverage(
        parts=len(parts), chars_seen=seen, chars_total=len(log_text),
        timeline_cut=timeline_cut, cut_from=cut_from,
        trailer_cut=len(trailer) < len(full_trailer),
        markers_seen=sum(map(_is_marker, _lines(trailer))),
        markers_total=sum(map(_is_marker, lines[tail:])),
    ))


def _part_prompt(day: str, index: int, part: _Part, plan: _Plan) -> str:
    return (f"Day log for {day}, part {index} of {len(plan.parts)}, covering {part.span()}. "
            f"{plan.coverage.note()}\n\n{part.text}")


def _merge_prompt(day: str, notes: list[str], plan: _Plan, autonomous: str) -> str:
    n = len(plan.parts)
    sections = [f"Notes on the day log for {day}, written part by part in time order. "
                f"{plan.coverage.note()}"]
    for index, (part, text) in enumerate(zip(plan.parts, notes, strict=True), start=1):
        sections.append(f"=== Part {index} of {n}, {part.span()} ===\n{text}")
    if plan.trailer.strip():
        sections.append(f"=== From the day log, after its timeline ===\n{plan.trailer.strip()}")
    if autonomous:
        sections.append(f"=== Autonomous activity sample ===\n{autonomous}")
    return "\n\n".join(sections)


def _capped_notes(notes: str) -> tuple[str, bool]:
    """A part's notes within ``_MAX_NOTES_CHARS``, in whole lines; True if any were cut."""
    if len(notes) <= _MAX_NOTES_CHARS:
        return notes, False
    kept = _whole_lines(notes, _MAX_NOTES_CHARS) or notes[:_MAX_NOTES_CHARS]
    return (f"{kept.rstrip()}\n[these notes were cut at {len(kept):,} "
            f"of their {len(notes):,} characters]"), True


def _digest_in_parts(day: str, plan: _Plan, autonomous: str,
                     attempts_log: list[dict[str, str]]) -> tuple[str, int]:
    """One notes call per part, in time order, then one merge call.

    Returns the digest and how many parts' notes were cut to fit the merge.
    """
    coverage = plan.coverage
    if coverage.cut:
        log.warning("digest %s in %d parts: %s", day, coverage.parts, coverage.note())
    else:
        log.info("digest %s: %d chars in %d parts", day, coverage.chars_total, coverage.parts)
    notes: list[str] = []
    notes_cut = 0
    for index, part in enumerate(plan.parts, start=1):
        text, was_cut = _capped_notes(
            compose(_PART_SYSTEM, _part_prompt(day, index, part, plan), attempts_log))
        notes.append(text)
        notes_cut += was_cut
    if notes_cut:
        log.warning("digest %s: the notes of %d parts were cut to %d chars for the merge",
                    day, notes_cut, _MAX_NOTES_CHARS)
    digest = compose(_MERGE_SYSTEM, _merge_prompt(day, notes, plan, autonomous), attempts_log)
    return digest, notes_cut


def digest_day(cfg: DaylogConfig, day: str, attempts_log: list[dict[str, str]]) -> Path | None:
    """Write ``DD-digest.md`` for *day*. Returns None if there is no log.

    A failed call (any part, or the merge) raises, and no digest is written.
    """
    turns = read_day_turns(cfg.day_jsonl(day))
    if not turns:
        return None
    rendered = cfg.day_md(day)
    try:
        log_text = rendered.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    autonomous = _autonomous_sample(turns)
    notes_cut = 0
    if len(log_text) <= _MAX_LOG_CHARS:
        # One call, the whole log: its trailer is inside the one call's budget.
        user = f"Day log for {day}:\n\n{log_text}"
        if autonomous:
            user += f"\n\nAutonomous activity sample:\n{autonomous}"
        text = compose(_SYSTEM, user, attempts_log)
        coverage = _Coverage(1, len(log_text), len(log_text))
    else:
        plan = _plan(log_text)
        text, notes_cut = _digest_in_parts(day, plan, autonomous, attempts_log)
        coverage = plan.coverage

    out = cfg.day_digest(day)
    tz = day_tz()
    fields = [
        "---",
        f"date: {day}",
        f"input_hash: {input_hash(turns)}",
        f"tz_offset: {tz.utcoffset(None)}",
        f"parts: {coverage.parts}",
        f"chars_seen: {coverage.chars_seen}",
        f"chars_total: {coverage.chars_total}",
        f"cut: {'true' if coverage.cut else 'false'}",
    ]
    if coverage.cut_from:
        fields.append(f'cut_from: "{coverage.cut_from}"')
    if coverage.markers_seen < coverage.markers_total:
        fields += [f"markers_seen: {coverage.markers_seen}",
                   f"markers_total: {coverage.markers_total}"]
    if notes_cut:
        fields.append(f"notes_cut: {notes_cut}")
    header ="\n".join([*fields, "generated_by: smriti nightly (digest)", "---", ""])
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(header + text + "\n", encoding="utf-8")
    tmp.replace(out)
    return out

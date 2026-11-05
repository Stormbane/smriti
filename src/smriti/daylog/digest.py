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
notes. What lies beyond the last part is cut, and the cut is never
silent: every prompt is told how much of the day it sees and where the
log was cut, and the digest's frontmatter records ``parts``,
``chars_seen``, ``chars_total``, ``cut`` and, for a cut day, ``cut_from``.
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
    "later part settles as settled. If you are told the log was cut, end with "
    "one line saying the digest covers the day only up to that time. " + _SECTIONS
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
    cut_from: str = ""  # HH:MM where the cut starts; "" when nothing is cut

    @property
    def cut(self) -> bool:
        return self.chars_seen < self.chars_total

    def note(self) -> str:
        """The prompts' line on how much of the day they see, and where it was cut."""
        if not self.cut:
            return f"The digest sees the whole day log ({self.chars_total:,} characters)."
        seen = (f"The digest sees {self.chars_seen:,} of the day log's "
                f"{self.chars_total:,} characters")
        if self.cut_from:
            return (f"{seen}: the log was cut at {self.cut_from}, and nothing after that "
                    "point reaches the digest.")
        return f"{seen}: the log was cut, and nothing after that point reaches the digest."


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


def _pieces(text: str, clock: str) -> list[_Block]:
    """One turn as blocks of at most a part's size: whole unless it alone is larger."""
    return [_Block(text[i:i + _MAX_LOG_CHARS], clock)
            for i in range(0, len(text), _MAX_LOG_CHARS)]


def _plan(log_text: str) -> _Plan:
    """Divide a rendered day log into at most ``_MAX_PARTS`` time-ordered parts.

    The log is a header, one block per turn (render.py sorts turns by
    time), then trailing sections. Parts are filled with whole turns in
    order, and only a turn larger than a part is split. The trailing
    sections (the autonomous-session markers) go to the merge call
    instead of a part, so a cut never drops them. What doesn't fit in
    the parts is cut, from the first turn left out.
    """
    lines = _lines(log_text)
    # The trailer starts at the first heading after the last turn, so a
    # stray heading-like line inside a turn can't pull turns out of order.
    last_turn = max((i for i, line in enumerate(lines) if _TURN_RE.match(line)), default=-1)
    tail = next((i for i in range(last_turn + 1, len(lines))
                 if lines[i].startswith(_SECTION_PREFIX)), len(lines))
    trailer = "".join(lines[tail:])
    by_turn: list[tuple[str, list[str]]] = []  # (clock, lines); the header comes first
    for line in lines[:tail]:
        match = _TURN_RE.match(line)
        if match or not by_turn:
            by_turn.append((match.group(1) if match else "", [line]))
        else:
            by_turn[-1][1].append(line)  # a continuation line, or the blank before a section

    groups: list[list[_Block]] = []
    size = 0
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
            cut_from = next((b.clock for b in blocks[index:] if b.clock), "")
            break

    parts: list[_Part] = []
    for group in groups:
        clocks = [b.clock for b in group if b.clock]
        parts.append(_Part("".join(b.text for b in group),
                           clocks[0] if clocks else "", clocks[-1] if clocks else ""))
    seen = sum(len(p.text) for p in parts) + len(trailer)
    return _Plan(tuple(parts), trailer,
                 _Coverage(len(parts), seen, len(log_text), cut_from))


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


def _digest_in_parts(day: str, plan: _Plan, autonomous: str,
                     attempts_log: list[dict[str, str]]) -> str:
    """One notes call per part, in time order, then one merge call."""
    coverage = plan.coverage
    if coverage.cut:
        log.warning("digest %s: the day log is %d chars; %d parts see %d, cut from %s",
                    day, coverage.chars_total, coverage.parts, coverage.chars_seen,
                    coverage.cut_from or "an unknown time")
    else:
        log.info("digest %s: %d chars in %d parts", day, coverage.chars_total, coverage.parts)
    notes = [compose(_PART_SYSTEM, _part_prompt(day, index, part, plan), attempts_log)
             for index, part in enumerate(plan.parts, start=1)]
    return compose(_MERGE_SYSTEM, _merge_prompt(day, notes, plan, autonomous), attempts_log)


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
    if len(log_text) <= _MAX_LOG_CHARS:
        user = f"Day log for {day}:\n\n{log_text}"
        if autonomous:
            user += f"\n\nAutonomous activity sample:\n{autonomous}"
        text = compose(_SYSTEM, user, attempts_log)
        coverage = _Coverage(1, len(log_text), len(log_text))
    else:
        plan = _plan(log_text)
        text = _digest_in_parts(day, plan, autonomous, attempts_log)
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
    header = "\n".join([*fields, "generated_by: smriti nightly (digest)", "---", ""])
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(header + text + "\n", encoding="utf-8")
    tmp.replace(out)
    return out

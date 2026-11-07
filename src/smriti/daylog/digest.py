"""The daily digest — one read-compose-emit LLM call per day, a few for a long day.

Primary seat: ``claude_cli``, a subscription seat. A
fallback seat, usually ``codex_cli``, only when configured
(:func:`nightly_fallback`; opt-in since hardening H12.2). The subprocess
is constrained to no tools/MCP — it holds no ``smriti_write`` license;
anything notable belongs in the digest file itself, which lives in the
tree and gets indexed.

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

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from smriti.core.memwrite import write_memory
from smriti.daylog.config import DEFAULT_AGENT_NAME, DEFAULT_USER_NAME, DaylogConfig
from smriti.daylog.model import USER, Turn, day_tz
from smriti.daylog.render import AUTONOMOUS_HEADING, DaySnapshot, input_hash, input_hash_of
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

# The one-call prompt names the two parties by their configured names.
_SYSTEM = (
    "You are {agent}'s nightly digest writer. You receive one day's complete "
    "communication log between {user} and {agent} across every channel (voice, "
    "Telegram, Claude Code sessions, Codex), plus a sample of autonomous "
    "session activity. "
)


def _system_prompt(cfg: DaylogConfig) -> str:
    """The one-call digest prompt, naming ``cfg``'s user and agent."""
    return _SYSTEM.format(user=cfg.user_name, agent=cfg.agent_name) + _SECTIONS

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
# continuation lines are indented. Render's AUTONOMOUS_HEADING, exactly,
# starts the section after the timeline (the autonomous-session markers):
# any other heading, even an unindented one, belongs to the turn it is in.
_TURN_RE = re.compile(r"- `(\d\d:\d\d)`")

# The compose subprocess reads untrusted text (captured transcripts), so it
# gets text in and text out, nothing else. tests/test_llm_tool_boundary.py
# runs the installed CLI with these flags; on Claude Code 2.1.283:
# - --strict-mcp-config, with no --mcp-config, drops every MCP server. Without
#   it the account's claude.ai connectors (mail, documents) load, even under
#   --setting-sources project,local. It comes first, so no flag can swallow it.
# - --tools= is the documented "no built-in tools". Its empty value is attached:
#   a separate "" that a wrapper dropped would leave the variadic --tools to
#   take the next flag as its value (--tools --strict-mcp-config loads the
#   connectors, and --tools --setting-sources drops the isolation).
# - --disallowedTools=* denies every tool as a second layer. On 2.1.283 it
#   alone empties the tool list too, but the wildcard isn't documented.
# - --disable-slash-commands removes the skills and slash commands.
_NO_TOOLS_ARGS = ["--strict-mcp-config", "--tools=", "--disallowedTools=*",
                  "--disable-slash-commands"]


def nightly_fallback(cfg: DaylogConfig | None) -> str:
    """The provider to fall back to when the primary fails, or "" for none.

    ``SMRITI_NIGHTLY_FALLBACK`` wins when it is set (empty: none); else
    ``"nightly_fallback"`` in daylog.json; else none. Until H12.2 it was
    ``codex_cli`` unless turned off, but codex's read-only sandbox still
    lets it read any file the user can, and run commands that only read,
    while it reads untrusted transcripts: a prompt injection in one could
    make it copy a local file into a digest. So it is opt-in.
    """
    env = os.environ.get("SMRITI_NIGHTLY_FALLBACK")
    if env is not None:
        return env.strip()
    return cfg.nightly_fallback if cfg is not None else ""


def _providers(cfg: DaylogConfig | None) -> list[str]:
    primary = os.environ.get("SMRITI_NIGHTLY_PROVIDER", "claude_cli")
    fallback = nightly_fallback(cfg)
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def prompt_fingerprint(cfg: DaylogConfig | None, *shaping: object) -> str:
    """What shapes a nightly artifact besides its inputs, as a short hash (H12.12).

    *shaping* is the artifact's own: its prompts and the budgets that decide
    what its call sees. To it come the provider chain and the two speakers'
    names. An artifact records this as ``prompt_fp``; one whose recorded
    fingerprint differs from the current one is stale. One that records
    none, written before H12.12, is judged without it.
    """
    user = cfg.user_name if cfg is not None else DEFAULT_USER_NAME
    agent = cfg.agent_name if cfg is not None else DEFAULT_AGENT_NAME
    parts = [*map(str, shaping), "providers=" + ",".join(_providers(cfg)),
             "user=" + user, "agent=" + agent]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def digest_fingerprint(cfg: DaylogConfig) -> str:
    """:func:`prompt_fingerprint` of a digest: all three of its prompts and its budgets."""
    return prompt_fingerprint(cfg, _system_prompt(cfg), _PART_SYSTEM, _MERGE_SYSTEM,
                              _MAX_LOG_CHARS, _MAX_PARTS, _MAX_AUTON_CHARS, _MAX_TRAILER_CHARS)


def compose(
    system: str,
    user: str,
    attempts_log: list[dict[str, str]],
    *,
    cfg: DaylogConfig | None = None,
    claude_cli_args: list[str] | None = None,
    providers: list[str] | None = None,
) -> str:
    """Run one read-compose-emit call through the provider chain.

    The chain is the primary and, when *cfg* or the environment sets one,
    the fallback (:func:`nightly_fallback`); without *cfg*, only the
    environment can. ``claude_cli_args`` overrides the default no-tools
    constraint for the claude_cli seat (the morning call allows web
    search). Such a call runs on claude_cli ONLY: the fallback seat can't
    be held to the same tool boundary (codex_cli keeps its shell), and a
    call that reads untrusted web results must not fall through to it.
    ``providers`` names the chain outright (the
    morning's fallback call, :func:`smriti.daylog.morning.compose_message`);
    a seat other than claude_cli gets no flags of smriti's, so its own
    isolation is all it has.
    """
    last_error: Exception | None = None
    if providers is not None:
        chain = list(providers)
    elif claude_cli_args is not None:
        chain = ["claude_cli"]
    else:
        chain = _providers(cfg)
    for name in chain:
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
        if any(t.who == USER for t in group):
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
    # The trailer starts at render's heading after the last turn, so neither a
    # heading inside a turn nor a stray turn-like line can move the boundary.
    last_turn = max((i for i, line in enumerate(lines) if _TURN_RE.match(line)), default=-1)
    tail = next((i for i in range(last_turn + 1, len(lines))
                 if lines[i].rstrip("\r\n") == AUTONOMOUS_HEADING), len(lines))
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


def _digest_in_parts(cfg: DaylogConfig, day: str, plan: _Plan, autonomous: str,
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
            compose(_PART_SYSTEM, _part_prompt(day, index, part, plan), attempts_log, cfg=cfg))
        notes.append(text)
        notes_cut += was_cut
    if notes_cut:
        log.warning("digest %s: the notes of %d parts were cut to %d chars for the merge",
                    day, notes_cut, _MAX_NOTES_CHARS)
    digest = compose(_MERGE_SYSTEM, _merge_prompt(day, notes, plan, autonomous), attempts_log,
                     cfg=cfg)
    return digest, notes_cut


def digest_day(cfg: DaylogConfig, day: str, attempts_log: list[dict[str, str]], *,
               snapshot: DaySnapshot | None = None) -> Path | None:
    """Write ``DD-digest.md`` for *day*. Returns None if there is no log.

    The digest records the input hash of the turns behind the text it
    summarised, so it is stale whenever that text is (H12.11). The nightly
    passes the *snapshot* it rendered the day from: the rendered text, the
    turns it was rendered from and their hash, all from one read. Without
    one, the digest summarises ``DD.md`` as it is and records the hash that
    file carries.

    A failed call (any part, or the merge) raises, and no digest is written.
    """
    if snapshot is not None:
        turns, digested = snapshot.turns, snapshot.input_hash
        log_text = snapshot.text
        if "\r" in log_text:  # as read_text would give it back from DD.md
            log_text = log_text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        turns = read_day_turns(cfg.day_jsonl(day))
        if not turns:
            return None
        try:
            log_text = cfg.day_md(day).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        digested = input_hash_of(log_text) or input_hash(turns)
    fingerprint = digest_fingerprint(cfg)  # what makes it, as it is before the call
    autonomous = _autonomous_sample(turns)
    notes_cut = 0
    if len(log_text) <= _MAX_LOG_CHARS:
        # One call, the whole log: its trailer is inside the one call's budget.
        user = f"Day log for {day}:\n\n{log_text}"
        if autonomous:
            user += f"\n\nAutonomous activity sample:\n{autonomous}"
        text = compose(_system_prompt(cfg), user, attempts_log, cfg=cfg)
        coverage = _Coverage(1, len(log_text), len(log_text))
    else:
        plan = _plan(log_text)
        text, notes_cut = _digest_in_parts(cfg, day, plan, autonomous, attempts_log)
        coverage = plan.coverage

    out = cfg.day_digest(day)
    tz = day_tz(cfg.root)
    fields = [
        "---",
        f"date: {day}",
        f"input_hash: {digested}",
        f"prompt_fp: {fingerprint}",
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
    # Masked: the model reads untrusted transcripts, and an injection can make
    # it copy a credential shape into the digest, which is indexed and pushed.
    write_memory(out, header + text + "\n")
    return out

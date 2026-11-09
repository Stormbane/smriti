"""Render a day's JSONL into the human-readable timeline (``DD.md``).

Derived artifacts carry an ``input_hash`` — the hash of the day's
sorted turn ids — in frontmatter, and a ``content_hash`` of the turns
themselves. The nightly repair pass compares them against the
current day-file to detect staleness; every derived step is re-runnable
from its inputs.

The nightly renders a day from one read of its turns
(:func:`render_snapshot`) and digests that rendered text, so a digest's
hash is the hash of the turns it summarised: a turn appended after the
read leaves both artifacts stale, never a digest that claims a turn its
text lacks.

Autonomous sessions (zero human turns in this day) collapse to one
marker line — derived here at render time, never written into the
captured record, so a human turn arriving later simply changes the
render.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from smriti.core.memwrite import write_memory
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import USER, Turn, day_tz
from smriti.daylog.writer import read_whole_day

_HASH_RE = re.compile(r"^input_hash:\s*(\S+)\s*$", re.MULTILINE)

# Starts the section after the timeline; the digest finds it by this line.
AUTONOMOUS_HEADING = "## Autonomous sessions"

# A derived artifact's frontmatter is in its first _HEAD_CHARS characters.
# _HEAD_BYTES always decode to more than that (a character is at most four
# bytes), so reading only them gives the head that reading the file would.
_HEAD_CHARS = 2000
_HEAD_BYTES = 8192


def input_hash(turns: list[Turn]) -> str:
    """The hash of the turns' ids: which turns a derived artifact was made from."""
    ids = sorted(t.id for t in turns)
    return hashlib.sha1("\n".join(ids).encode("utf-8")).hexdigest()[:16]


# The frontmatter field of :func:`content_hash`.
CONTENT_FIELD = "content_hash"


def content_hash(turns: list[Turn]) -> str:
    """The hash of the turns as stored: each one's id, time, channel, speaker, text, session.

    :func:`input_hash` hashes the ids alone, and a turn's id is fixed when it
    is captured, so a turn edited in place (a correction, a redaction) kept
    the day's input hash, and its old render and digest would stay
    current. Renders and digests record this as ``content_hash`` beside
    ``input_hash``, and one that records it is stale when it differs. One
    written before artifacts carried it records none and is judged by its
    input hash alone, so the upgrade regenerates nothing.
    """
    # repr: each field quoted and escaped, so no two turns' fields run together.
    records = sorted(repr((t.id, t.ts.isoformat(), t.channel, t.who, t.session, t.text))
                     for t in turns)
    return hashlib.sha1("\n".join(records).encode("utf-8")).hexdigest()[:16]


def head_of(data: bytes) -> str:
    """The head of a file whose bytes start with *data*, as ``read_text`` would give it."""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return text[:_HEAD_CHARS]


def read_head(path: Path) -> str:
    """The head of a derived artifact: its frontmatter and a little more ('' if unreadable)."""
    try:
        with path.open("rb") as f:
            return head_of(f.read(_HEAD_BYTES))
    except OSError:
        return ""


def head_field(head: str, name: str) -> str:
    """The value of frontmatter field *name* in *head* ('' if absent)."""
    m = re.search(rf"^{re.escape(name)}:\s*(\S+)\s*$", head, re.MULTILINE)
    return m.group(1) if m else ""


def input_hash_of(text: str) -> str:
    """The input_hash recorded in a derived artifact's text ('' if absent)."""
    m = _HASH_RE.search(text[:_HEAD_CHARS])
    return m.group(1) if m else ""


def read_input_hash(path: Path) -> str:
    """The input_hash recorded in a derived artifact ('' if absent)."""
    return input_hash_of(read_head(path))


def content_hash_of(text: str) -> str:
    """The content_hash recorded in a derived artifact's text ('' if absent: one
    written before artifacts carried it)."""
    return head_field(text[:_HEAD_CHARS], CONTENT_FIELD)


def head_is_of(head: str, current: str, content: str) -> bool:
    """Whether a derived artifact's *head* says it was made from the turns whose
    input hash is *current* and content hash is *content*.

    The input hash must match. The content hash must too when the head
    records one; a head written before artifacts carried it is judged by its
    input hash alone.
    """
    if input_hash_of(head) != current:
        return False
    recorded = head_field(head, CONTENT_FIELD)
    return not recorded or recorded == content


def _split_autonomous(turns: list[Turn]) -> tuple[list[Turn], list[list[Turn]]]:
    by_session: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        by_session.setdefault((t.channel, t.session), []).append(t)
    conversational: list[Turn] = []
    autonomous: list[list[Turn]] = []
    for group in by_session.values():
        if any(t.who == USER for t in group):
            conversational.extend(group)
        else:
            autonomous.append(group)
    conversational.sort(key=lambda t: (t.ts, t.id))
    autonomous.sort(key=lambda g: g[0].ts)
    return conversational, autonomous


@dataclass(frozen=True)
class DaySnapshot:
    """One read of a day: its turns, their hash, and the timeline rendered from exactly them."""

    day: str
    turns: list[Turn]
    input_hash: str
    text: str  # DD.md as written (masked), with "\n" line endings
    path: Path
    content_hash: str = ""  # the turns' content_hash


def render_text(cfg: DaylogConfig, day: str, turns: list[Turn], digest: str,
                content: str = "") -> str:
    """The timeline of *turns*, with *digest*, their input hash, and *content*, their
    content hash, in its frontmatter."""
    conversational, autonomous = _split_autonomous(turns)
    tz = day_tz(cfg.root)

    lines = [
        "---",
        f"date: {day}",
        f"input_hash: {digest}",
        *([f"{CONTENT_FIELD}: {content}"] if content else []),
        "generated_by: smriti nightly (render)",
        "---",
        "",
        f"# Day log — {day}",
        "",
    ]
    last_channel = ""
    for t in conversational:
        clock = t.ts.astimezone(tz).strftime("%H:%M")
        channel = f" [{t.channel}]" if t.channel != last_channel else ""
        last_channel = t.channel
        speaker = cfg.speaker(t.who)
        # Every line break, \r included, so each continuation line is indented
        # (a lone \r would come back from read_text as an unindented line).
        text = t.text.strip().replace("\r\n", "\n").replace("\r", "\n")
        indented = text.replace("\n", "\n  ")
        lines.append(f"- `{clock}`{channel} **{speaker}:** {indented}")
    if autonomous:
        lines += ["", AUTONOMOUS_HEADING, ""]
        for group in autonomous:
            start = group[0].ts.astimezone(tz).strftime("%H:%M")
            end = group[-1].ts.astimezone(tz).strftime("%H:%M")
            lines.append(
                f"- [autonomous: {group[0].channel}, {start}–{end}, {len(group)} turns]"
            )
    return "\n".join(lines) + "\n"


def render_snapshot(cfg: DaylogConfig, day: str) -> DaySnapshot | None:
    """Read *day*'s turns once, write ``DD.md`` from them, and return that read.

    None if the day has no turns. A read that fails partway raises
    :class:`~smriti.daylog.writer.DayUnreadable` and writes nothing, so the
    render already there stays.
    """
    turns = read_whole_day(cfg.day_jsonl(day))
    if not turns:
        return None
    digest = input_hash(turns)
    content = content_hash(turns)
    out = cfg.day_md(day)
    # Masked like every write of memory text. Capture masked each turn already;
    # this catches days captured before a pattern existed.
    text = write_memory(out, render_text(cfg, day, turns, digest, content))
    return DaySnapshot(day=day, turns=turns, input_hash=digest, text=text, path=out,
                       content_hash=content)


def render_day(cfg: DaylogConfig, day: str) -> Path | None:
    """Write ``DD.md`` for *day* (YYYY-MM-DD). Returns None if no turns.

    As :func:`render_snapshot`, a day-file that can't be read to its end raises.
    """
    snapshot = render_snapshot(cfg, day)
    return snapshot.path if snapshot is not None else None

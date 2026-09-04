"""Render a day's JSONL into the human-readable timeline (``DD.md``).

Derived artifacts carry an ``input_hash`` — the hash of the day's
sorted turn ids — in frontmatter. The nightly repair pass compares that
hash against the current day-file to detect staleness; every derived
step is re-runnable from its inputs.

Autonomous sessions (zero human turns in this day) collapse to one
marker line — derived here at render time, never written into the
captured record, so a human turn arriving later simply changes the
render (spec: review round 1, finding 4).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, day_tz
from smriti.daylog.writer import read_day_turns

_HASH_RE = re.compile(r"^input_hash:\s*(\S+)\s*$", re.MULTILINE)


def input_hash(turns: list[Turn]) -> str:
    ids = sorted(t.id for t in turns)
    return hashlib.sha1("\n".join(ids).encode("utf-8")).hexdigest()[:16]


def read_input_hash(path: Path) -> str:
    """The input_hash recorded in a derived artifact ('' if absent)."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""
    m = _HASH_RE.search(head)
    return m.group(1) if m else ""


def _split_autonomous(turns: list[Turn]) -> tuple[list[Turn], list[list[Turn]]]:
    by_session: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        by_session.setdefault((t.channel, t.session), []).append(t)
    conversational: list[Turn] = []
    autonomous: list[list[Turn]] = []
    for group in by_session.values():
        if any(t.who == "suti" for t in group):
            conversational.extend(group)
        else:
            autonomous.append(group)
    conversational.sort(key=lambda t: (t.ts, t.id))
    autonomous.sort(key=lambda g: g[0].ts)
    return conversational, autonomous


def render_day(cfg: DaylogConfig, day: str) -> Path | None:
    """Write ``DD.md`` for *day* (YYYY-MM-DD). Returns None if no turns."""
    turns = read_day_turns(cfg.day_jsonl(day))
    if not turns:
        return None
    conversational, autonomous = _split_autonomous(turns)
    tz = day_tz()

    lines = [
        "---",
        f"date: {day}",
        f"input_hash: {input_hash(turns)}",
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
        speaker = "Suti" if t.who == "suti" else "Narada"
        text = t.text.strip().replace("\r\n", "\n")
        indented = text.replace("\n", "\n  ")
        lines.append(f"- `{clock}`{channel} **{speaker}:** {indented}")
    if autonomous:
        lines += ["", "## Autonomous sessions", ""]
        for group in autonomous:
            start = group[0].ts.astimezone(tz).strftime("%H:%M")
            end = group[-1].ts.astimezone(tz).strftime("%H:%M")
            lines.append(
                f"- [autonomous: {group[0].channel}, {start}–{end}, {len(group)} turns]"
            )
    out = cfg.day_md(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out

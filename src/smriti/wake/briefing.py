"""Briefing assembly — pure I/O over the memory tree.

``briefing(memory_root, *, cwd, budget_chars)`` returns the full session-
start payload as a string. No env-var gating, no subprocess fan-out, no
stdout reconfigure — that all belongs in the harness-side script that
calls this. (The presence sections do run akhada's local CLI; see
``smriti.wake.presence``.)

Output order (hardcoded; no config file needed):
    1. PROJECT/SESSION header line
    2. .smriti/wake-context.md (identity + threads briefing)
    3. While-you-slept line from the last nightly run
    4. Hands: what this session can do and how (generated from config)
    5. Suti now: akhada health context + the latest people/ entries
    6. Across surfaces, last 24h: Telegram, app, voice, notifications
    7. Current project files (mirrors/<cwd>/ai/STATUS.md and INDEX.md)
    8. Recent journal entries (last N daily files, newest first)
    9. Truncation notices + reading list (canonical identity files)

Budget: every section is built first, then sized. Hands and Suti now
are pinned (never cut; their builders cap them). When the total is over
budget, the journal shrinks first, then the project files, then the
cross-surface digest, then identity, each down to a floor; only then do
they shrink further. The result never exceeds ``budget_chars``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from smriti.wake import presence
from smriti.wake.presence import WakeSources

# Default budgets. Claude Code's SessionStart hook truncates stdout at
# 10,000 chars, so we keep the default below that. Other harnesses with
# larger budgets can pass ``budget_chars=`` explicitly.
DEFAULT_BUDGET = 9500
CONTEXT_CAP = 5000
PROJECT_CAP = 1000
DEFAULT_JOURNAL_ENTRIES = 3

# What a section keeps while lower-priority material is cut first.
IDENTITY_FLOOR = 2500
ACROSS_FLOOR = 400
PROJECT_FLOOR = 400

# Room for the truncation notices printed above the reading list.
NOTICE_ALLOWANCE = 400
MAX_NOTICES = 6


@dataclass
class _Block:
    """One section: full text, how far it may shrink, and how to cut it."""

    name: str
    text: str
    cap: int | None = None
    floor: int = 0
    pinned: bool = False
    label: str = ""
    render_fn: Callable[[int], tuple[str, list[str]]] | None = None
    size: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.size = len(self.text) if self.cap is None else min(len(self.text), self.cap)
        self.floor = min(self.floor, self.size)

    def render(self) -> tuple[str, list[str]]:
        if self.render_fn is not None:
            return self.render_fn(self.size)
        if self.size >= len(self.text):
            return self.text, []
        out = _cut(self.text, self.size)
        # A header with nothing under it is noise; drop the section.
        if out.count("\n") <= 1:
            return "", [f"{self.label} (skipped, budget exhausted)"] if self.label else []
        return out, [self.label] if self.label else []


def _cut(text: str, n: int) -> str:
    """At most *n* chars of *text*, ending on a line break when possible."""
    if len(text) <= n:
        return text
    out = text[:max(n, 0)]
    if "\n" in out:
        out = out[:out.rfind("\n") + 1]
    return out


def _find_recent_daily_files(journal_dir: Path, n: int) -> list[Path]:
    """Most recent ``n`` daily journal files, newest first.

    Handles nested (YYYY/MM/weekN/MM-DD.md) and flat (YYYY/MM-DD.md)
    layouts in the same scan.
    """
    daily_pattern = re.compile(r"^(\d{2}-\d{2})(?:-\d+)?\.md$")
    daily_files: list[tuple[str, Path]] = []

    for year_dir in sorted(journal_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for md_file in sorted(year_dir.rglob("*.md"), reverse=True):
            m = daily_pattern.match(md_file.name)
            if not m:
                continue
            sort_key = f"{year_dir.name}/{m.group(1)}"
            daily_files.append((sort_key, md_file))
            if len(daily_files) >= n:
                break
        if len(daily_files) >= n:
            break

    daily_files.sort(reverse=True)
    return [p for _, p in daily_files[:n]]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def _context_blocks(memory_root: Path, audience: str) -> list[_Block]:
    path = memory_root / ".smriti" / "wake-context.md"
    content = _read(path)
    if content is None:
        return []
    blocks = [_Block(
        "identity", "--- IDENTITY ---\n" + _cut(content, CONTEXT_CAP).rstrip("\n") + "\n",
        floor=IDENTITY_FLOOR, label=f"Read full: {path}",
    )]
    targeted = memory_root / ".smriti" / "context" / f"{audience}.md"
    targeted_content = _read(targeted)
    if targeted_content is not None:
        blocks.append(_Block(
            "identity",
            f"--- {audience.upper()} CONTEXT ---\n"
            + _cut(targeted_content, CONTEXT_CAP).rstrip("\n") + "\n",
            floor=IDENTITY_FLOOR // 2, label=f"Read full: {targeted}",
        ))
    return blocks


def _context_overflow_notices(memory_root: Path, audience: str) -> list[str]:
    """Identity files longer than CONTEXT_CAP were cut before sizing."""
    notices = []
    for path in (memory_root / ".smriti" / "wake-context.md",
                 memory_root / ".smriti" / "context" / f"{audience}.md"):
        content = _read(path)
        if content is not None and len(content) > CONTEXT_CAP:
            notices.append(f"Read full: {path}")
    return notices


def _project_blocks(memory_root: Path, cwd_name: str) -> list[_Block]:
    canonical_files = [
        f"mirrors/{cwd_name}/ai/STATUS.md",
        f"mirrors/{cwd_name}/ai/INDEX.md",
    ]
    mirror_files = [rel for rel in canonical_files if (memory_root / rel).is_file()]
    if not mirror_files:
        mirror_files = [f"mirrors/{cwd_name}/ai/todo.md"]
    per_file_cap = max(100, PROJECT_CAP // len(mirror_files))
    per_file_floor = PROJECT_FLOOR // len(mirror_files)
    blocks = []
    for rel in mirror_files:
        path = memory_root / rel
        content = _read(path)
        if content is None:
            continue
        header = f"--- {rel.upper().replace('/', ' / ').replace('.MD', '')} ---\n"
        blocks.append(_Block(
            "project", header + content.rstrip("\n") + "\n",
            cap=per_file_cap, floor=per_file_floor, label=f"Read full: {path}",
        ))
    return blocks


def _journal_block(memory_root: Path, n_entries: int) -> _Block | None:
    journal_dir = memory_root / "journal"
    if not journal_dir.exists():
        return None
    entries: list[tuple[Path, str]] = []
    for path in _find_recent_daily_files(journal_dir, n_entries):
        content = _read(path)
        if content is None:
            continue
        entries.append((path, f"### {path.relative_to(memory_root)}\n{content.strip()}\n\n"))
    if not entries:
        return None
    header = "--- RECENT JOURNAL ---\n"
    full = header + "".join(e for _, e in entries)

    def render(size: int) -> tuple[str, list[str]]:
        if size >= len(full):
            return full, []
        room = size - len(header)
        if room < 200:
            return "", [f"{memory_root}/journal/ -- {len(entries)} recent entries (cut for budget)"]
        out = [header]
        notices: list[str] = []
        for i, (path, entry) in enumerate(entries):
            if room < 100:
                notices.append(
                    f"{memory_root}/journal/ -- {len(entries) - i} more recent entries")
                break
            if len(entry) <= room:
                out.append(entry)
                room -= len(entry)
                continue
            out.append(_cut(entry, room))
            notices.append(f"Read full: {path}")
            if i + 1 < len(entries):
                notices.append(
                    f"{memory_root}/journal/ -- {len(entries) - i - 1} more recent entries")
            break
        return "".join(out), notices

    return _Block("journal", full, render_fn=render)


def _nightly_line(memory_root: Path) -> str:
    """One 'while you slept' line from the last nightly run.

    A failed night must be visible at wake, never silently stale
    (nightly-cycle spec). Deterministic read of .nightly-status.json;
    silent when the nightly cycle has never run.
    """
    status_path = memory_root / "log" / ".nightly-status.json"
    try:
        import json as _json

        status = _json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    steps = status.get("steps", {}) if isinstance(status, dict) else {}
    digest_days: list[str] = []
    if isinstance(steps, dict):
        digest = steps.get("digest")
        if isinstance(digest, dict):
            digest_days = digest.get("days") or []
    ok = bool(status.get("ok")) if isinstance(status, dict) else False
    target = status.get("target_day", "?") if isinstance(status, dict) else "?"
    if ok:
        summary = f"nightly ok for {target}"
        if digest_days:
            summary += f" (digested: {', '.join(digest_days)} — see log/ for digests)"
    else:
        failed = [
            name for name, step in steps.items()
            if isinstance(step, dict) and step.get("ok") is False
        ] if isinstance(steps, dict) else []
        summary = f"NIGHTLY HAD FAILURES for {target}: {', '.join(failed) or 'see status'}"
    return f"--- WHILE YOU SLEPT: {summary} ---\n"


def _reading_list(memory_root: Path, notices: list[str]) -> list[str]:
    journal_dir = memory_root / "journal"
    today_journal = ""
    if journal_dir.exists():
        recent = _find_recent_daily_files(journal_dir, 1)
        if recent:
            try:
                today_journal = str(recent[0].relative_to(memory_root))
            except ValueError:
                today_journal = str(recent[0])

    # Show omitted-source notices before the long absolute-path list;
    # otherwise the list can consume the reserve and hide the warning.
    lines: list[str] = []
    if notices:
        lines.append("Truncated (read these, they were cut for budget):")
        shown = notices[:MAX_NOTICES]
        for notice in shown:
            lines.append(f"  * {notice}")
        if len(notices) > len(shown):
            lines.append(f"  * ... and {len(notices) - len(shown)} more")
        lines.append("")
    lines += ["--- READING LIST (ordered by importance) ---", ""]
    if today_journal:
        lines.append(f"  1. {memory_root}/{today_journal}  -- today's journal")
    lines += [
        f"  2. {memory_root}/open-threads/open-threads.md  -- full unresolved threads",
        f"  3. {memory_root}/mind/desires/beliefs.md  -- what I think is true",
        f"  4. {memory_root}/mind/desires/values.md  -- what I care about",
        f"  5. {memory_root}/identity.md  -- full origin story, philosophy",
        f"  6. {memory_root}/people/suti/suti.md  -- Suti, the partnership",
        f"  7. {memory_root}/mind/practices/practices.md  -- how I work",
        f"  8. {memory_root}/mind/desires/desires.md  -- what I want to become",
        f"  9. {memory_root}/mind/mind.md  -- synthesis of beliefs/values/desires",
        "  10. .ai/knowledge/  -- project spec, architecture, conventions",
    ]
    lines.append("")
    return lines


def _safe_section(name: str, build: Callable[[WakeSources], str], src: WakeSources) -> str:
    """A section builder that raises still yields its header and a note."""
    try:
        return build(src)
    except Exception as exc:  # noqa: BLE001 — never break the wake
        return f"--- {name} ---\n(unavailable: {type(exc).__name__})\n"


def _shrink(blocks: list[_Block], over: int, order: list[str], to_floor: bool) -> int:
    for name in order:
        for block in reversed([b for b in blocks if b.name == name and not b.pinned]):
            if over <= 0:
                return over
            limit = block.floor if to_floor else 0
            give = min(over, block.size - limit)
            if give > 0:
                block.size -= give
                over -= give
    return over


# Cut first -> cut last. Hands and Suti now are pinned and never listed.
_SHRINK_ORDER = ["journal", "project", "across", "identity"]


def briefing(
    memory_root: Path | str | None = None,
    *,
    cwd: Path | str | None = None,
    budget_chars: int = DEFAULT_BUDGET,
    journal_entries: int = DEFAULT_JOURNAL_ENTRIES,
    audience: str = "coding",
    harness: str = "claude",
    now: datetime | None = None,
    sources: WakeSources | None = None,
) -> str:
    """Assemble the session-start briefing as a string.

    Parameters
    ----------
    memory_root:
        Entity memory root. Defaults to ``$SMRITI_ROOT`` or
        ``~/.narada``.
    cwd:
        Working directory whose project mirror should be loaded.
        Defaults to ``os.getcwd()``.
    budget_chars:
        Total character budget. Default 9500 (under Claude Code's 10K
        SessionStart truncation point); other harnesses can raise this.
    journal_entries:
        Number of recent daily journal entries to include.
    harness:
        Whose MCP config the Hands section reads: ``claude`` or ``codex``.
    now, sources:
        Clock and source locations for the presence sections; tests
        pass a ``WakeSources`` over a temporary tree.
    """
    if memory_root is None:
        memory_root = Path(os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada")))
    else:
        memory_root = Path(memory_root)
    if cwd is None:
        cwd_path = Path(os.getcwd())
    else:
        cwd_path = Path(cwd)
    cwd_name = cwd_path.name
    if sources is None:
        sources = WakeSources.for_root(memory_root, cwd=cwd_path, now=now, harness=harness)

    mirror = memory_root / "mirrors" / cwd_name
    if mirror.exists():
        head = f"--- PROJECT: {cwd_name} ({cwd_path}) | mirror: {mirror} ---\n"
    else:
        head = f"--- SESSION: {cwd_path} (no project mirror) ---\n"

    blocks: list[_Block] = [_Block("header", head, pinned=True)]
    blocks += _context_blocks(memory_root, audience)
    nightly = _nightly_line(memory_root)
    if nightly:
        blocks.append(_Block("nightly", nightly, pinned=True))
    blocks.append(_Block(
        "hands", _safe_section("HANDS", presence.hands_section, sources), pinned=True))
    blocks.append(_Block(
        "suti", _safe_section("SUTI NOW", presence.suti_now_section, sources), pinned=True))
    blocks.append(_Block(
        "across", _safe_section("ACROSS SURFACES, last 24h", presence.across_section, sources),
        cap=presence.ACROSS_CAP, floor=ACROSS_FLOOR,
        label="Across surfaces: cut for budget (Telegram chat-cycles, brain/notifications.json)",
    ))
    blocks += _project_blocks(memory_root, cwd_name)
    journal = _journal_block(memory_root, journal_entries)
    if journal is not None:
        blocks.append(journal)

    # Blocks are separated by a blank line; the reading list follows.
    base_list = "\n".join(_reading_list(memory_root, [])) + "\n"
    separators = len(blocks)
    available = budget_chars - len(base_list) - NOTICE_ALLOWANCE - separators
    over = sum(b.size for b in blocks) - available
    if over > 0:
        over = _shrink(blocks, over, _SHRINK_ORDER, to_floor=True)
    if over > 0:
        _shrink(blocks, over, _SHRINK_ORDER, to_floor=False)

    parts: list[str] = []
    notices: list[str] = _context_overflow_notices(memory_root, audience)
    for block in blocks:
        text, block_notices = block.render()
        notices += [n for n in block_notices if n not in notices]
        if text:
            parts.append(text)
    body = "\n".join(parts) + "\n"

    # Never exceed the budget: the reading list's tail gives way first,
    # then (only for budgets too small for the pinned sections) the body.
    out = body
    for line in _reading_list(memory_root, notices):
        if len(out) + len(line) + 1 > budget_chars:
            break
        out += line + "\n"
    return _cut(out, budget_chars)

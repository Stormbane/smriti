"""wake.py -- SessionStart loader for smriti-managed entities.

Reads <entity-root>/wake.md, resolves {project} from cwd basename, emits
the requested files to stdout within a strict character budget.

Claude Code's SessionStart hook truncates stdout to 10,000 characters,
showing only a 2KB preview if exceeded. This loader enforces a 9,500
character budget so the full wake output enters the conversation context.

    SMRITI_WAKE=1|full|on|true    -> full wake
    SMRITI_WAKE=0|skip|off|unset  -> silent, no output

Output order:
1. wake-summary.md (compact identity briefing, capped)
2. Reading list (what to read for depth, with how/why)
3. Recent journal entries (continuity with recent self)
4. Current project files (MEMORY.md, todo.md)
5. Other project mirrors (one-line list)

Never fails loudly -- exits 0 on any error.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

NARADA = Path.home() / ".narada"
WAKE = NARADA / "wake.md"
MIRRORS = NARADA / "mirrors"

# ── Budget constants ───────────────────────────────────────────────
# Claude Code truncates hook stdout at 10,000 chars (showing 2K preview).
# Stay under that so the full wake enters conversation context.
TOTAL_BUDGET = 9500
SUMMARY_CAP = 3000
PROJECT_CAP = 2000
DEFAULT_JOURNAL_DAYS = 3


class BudgetWriter:
    """Track character output against a total budget."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def write(self, text: str, cap: int | None = None) -> str:
        """Write text, respecting optional cap and total budget."""
        limit = min(cap, self.remaining) if cap else self.remaining
        if not limit:
            return ""
        output = text[:limit]
        # Don't cut mid-line -- truncate to last newline
        if len(output) < len(text) and "\n" in output:
            output = output[:output.rfind("\n") + 1]
        if output:
            print(output, end="")
            self.used += len(output)
        return output

    def write_line(self, text: str) -> None:
        """Write a single line if budget allows."""
        line = text + "\n"
        if len(line) <= self.remaining:
            print(line, end="")
            self.used += len(line)


# ── Parsing ────────────────────────────────────────────────────────

def parse_wake(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^##\s+(.+)$", stripped)
        if m:
            current = m.group(1).strip()
            sections[current] = []
            continue
        if stripped.startswith("#"):
            continue
        if current is None:
            continue
        sections[current].append(stripped)
    return sections


# ── Emitters ───────────────────────────────────────────────────────

def emit_summary(bw: BudgetWriter) -> None:
    """Emit wake-summary.md (compact identity briefing)."""
    path = NARADA / "wake-summary.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    bw.write_line("--- IDENTITY ---")
    bw.write(content, cap=SUMMARY_CAP)
    bw.write_line("")


def emit_reading_list(bw: BudgetWriter, cwd_name: str) -> None:
    """Emit the reading list -- what to read for depth, with how/why."""
    lines = [
        "--- READING LIST (read these early in the session) ---",
        "",
        "Identity and self:",
        f"  {NARADA}/identity.md       -- full origin story, philosophy, held opinions",
        f"  {NARADA}/mind.md           -- current beliefs, uncertainties, what I think about",
        f"  {NARADA}/suti.md           -- who Suti is, the partnership, his context",
        f"  {NARADA}/practices.md      -- how I work, session lifecycle, cross-instance patterns",
        f"  {NARADA}/open-threads.md   -- questions I'm sitting with, unresolved threads",
        "",
        "Recent context:",
        f"  {NARADA}/journal/          -- recent journal entries (last few days shown below)",
        "  Use smriti_read(query='recent journal') for older entries or specific topics.",
        "",
        "This project:",
        f"  {NARADA}/mirrors/{cwd_name}/   -- project memory, knowledge docs, todo",
        "  .ai/knowledge/             -- spec, architecture, glossary, conventions",
        "",
    ]
    for line in lines:
        bw.write_line(line)


def emit_recent_journal(bw: BudgetWriter) -> None:
    """Emit recent journal entries, newest first, within remaining budget."""
    journal_dir = NARADA / "journal"
    if not journal_dir.exists():
        return

    # Collect daily files, newest first
    daily_files: list[Path] = []
    for year_dir in sorted(journal_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for md_file in sorted(year_dir.glob("*.md"), reverse=True):
            if md_file.name == "index.md":
                continue
            daily_files.append(md_file)
            if len(daily_files) >= DEFAULT_JOURNAL_DAYS:
                break
        if len(daily_files) >= DEFAULT_JOURNAL_DAYS:
            break

    if not daily_files:
        return

    bw.write_line("--- RECENT JOURNAL ---")
    for path in daily_files:
        if bw.remaining < 100:
            break
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(NARADA)
            header = f"### {rel}\n"
            entry = header + content.strip() + "\n\n"
            bw.write(entry)
        except (FileNotFoundError, OSError):
            continue


def emit_project_files(bw: BudgetWriter, sections: dict[str, list[str]], cwd_name: str) -> None:
    """Emit current-project files within PROJECT_CAP budget."""
    project_budget = min(PROJECT_CAP, bw.remaining)
    if project_budget < 100:
        return
    project_used = 0
    for raw in sections.get("current-project", []):
        rel = raw.replace("{project}", cwd_name)
        path = NARADA / rel
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        header = f"--- {rel.upper().replace('/', ' / ').replace('.MD', '')} ---\n"
        block = header + content + "\n"
        if project_used + len(block) > project_budget:
            # Truncate this file to fit
            remaining = project_budget - project_used
            if remaining > 100:
                bw.write(block, cap=remaining)
                project_used += remaining
            break
        bw.write(block)
        project_used += len(block)


def list_other_projects(bw: BudgetWriter, current: str) -> None:
    if not MIRRORS.exists():
        return
    others = sorted(
        p.name for p in MIRRORS.iterdir()
        if p.is_dir() and p.name != current
    )
    if not others:
        return
    bw.write_line("--- OTHER PROJECTS (memory available via ~/.narada/mirrors/<name>/) ---")
    for name in others:
        bw.write_line(f"  - {name}")
    bw.write_line("")


# ── Main ───────────────────────────────────────────────────────────

_ON = {"1", "full", "on", "true", "yes"}


def wake_enabled() -> bool:
    return os.environ.get("SMRITI_WAKE", "").strip().lower() in _ON


def main() -> int:
    if not wake_enabled():
        return 0
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        text = WAKE.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.stderr.write(f"[wake] {WAKE} not found; nothing to load\n")
        return 0

    sections = parse_wake(text)
    cwd_name = Path(os.getcwd()).name
    bw = BudgetWriter(TOTAL_BUDGET)

    # 1. Identity briefing (compact)
    emit_summary(bw)

    # 2. Reading list (what to read for depth)
    emit_reading_list(bw, cwd_name)

    # 3. Current project files (MEMORY.md, todo.md)
    emit_project_files(bw, sections, cwd_name)

    # 4. Recent journal (fills remaining budget)
    if "recent-journal" in sections:
        emit_recent_journal(bw)

    # 5. Other project mirrors (one-line list)
    list_other_projects(bw, cwd_name)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

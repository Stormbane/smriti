"""wake.py -- SessionStart loader for smriti-managed entities.

Reads <entity-root>/wake.md, resolves {project} from cwd basename, emits
the requested files to stdout within a strict character budget.

Claude Code's SessionStart hook truncates stdout at 10,000 characters,
showing only a 2KB preview if exceeded. This loader enforces a 9,500
character budget so the full wake output enters the conversation context.

    SMRITI_WAKE=1|full|on|true    -> full wake
    SMRITI_WAKE=0|skip|off|unset  -> silent, no output

Output order:
1. .smriti/wake-summary.md (compact identity briefing, capped)
2. Current project files (MEMORY.md, todo.md)
3. Recent journal entries (continuity with recent self)
4. Reading list (what to read for depth, with truncation notices)
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
        self.truncated: list[str] = []  # files that were truncated

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def write(self, text: str, cap: int | None = None, label: str = "") -> bool:
        """Write text, respecting optional cap and total budget.

        Returns True if the full text was emitted, False if truncated.
        """
        limit = min(cap, self.remaining) if cap else self.remaining
        if not limit:
            if label:
                self.truncated.append(f"{label} (skipped, budget exhausted)")
            return False
        output = text[:limit]
        was_truncated = len(output) < len(text)
        # Don't cut mid-line -- truncate to last newline
        if was_truncated and "\n" in output:
            output = output[:output.rfind("\n") + 1]
        if output:
            print(output, end="")
            self.used += len(output)
        if was_truncated and label:
            self.truncated.append(label)
        return not was_truncated

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
    """Emit .smriti/wake-summary.md (compact identity briefing)."""
    path = NARADA / ".smriti" / "wake-summary.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    bw.write_line("--- IDENTITY ---")
    bw.write(content, cap=SUMMARY_CAP, label=f"Read full: {path}")
    bw.write_line("")


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
        remaining_project = project_budget - project_used
        if len(block) > remaining_project:
            if remaining_project > 100:
                bw.write(block, cap=remaining_project, label=f"Read full: {path}")
                project_used += remaining_project
            else:
                bw.truncated.append(f"Read full: {path} (skipped, project budget exhausted)")
            break
        bw.write(block)
        project_used += len(block)


def _find_recent_daily_files(journal_dir: Path, n: int) -> list[Path]:
    """Find the most recent N daily journal files.

    Handles both the new nested structure (YYYY/MM/weekN/MM-DD.md) and
    the old flat structure (YYYY/MM-DD.md) for backward compatibility.
    Daily files match the MM-DD.md pattern. Summary files (weekN.md,
    MM.md, YYYY.md, index.md) are excluded.
    """
    import re
    daily_pattern = re.compile(r"^\d{2}-\d{2}\.md$")
    daily_files: list[tuple[str, Path]] = []

    for year_dir in sorted(journal_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        # Recursively find all MM-DD.md files under this year
        for md_file in sorted(year_dir.rglob("*.md"), reverse=True):
            if not daily_pattern.match(md_file.name):
                continue
            # Sort key: YYYY/MM-DD (works lexicographically)
            sort_key = f"{year_dir.name}/{md_file.stem}"
            daily_files.append((sort_key, md_file))
            if len(daily_files) >= n:
                break
        if len(daily_files) >= n:
            break

    daily_files.sort(reverse=True)
    return [p for _, p in daily_files[:n]]


def emit_recent_journal(bw: BudgetWriter) -> None:
    """Emit recent journal entries, newest first, within remaining budget."""
    journal_dir = NARADA / "journal"
    if not journal_dir.exists():
        return

    daily_files = _find_recent_daily_files(journal_dir, DEFAULT_JOURNAL_DAYS)

    if not daily_files:
        return

    bw.write_line("--- RECENT JOURNAL ---")
    emitted = 0
    for path in daily_files:
        if bw.remaining < 100:
            remaining_count = len(daily_files) - emitted
            if remaining_count > 0:
                bw.truncated.append(
                    f"{NARADA}/journal/ -- {remaining_count} more recent entries "
                    f"(budget exhausted, use smriti_read for older entries)"
                )
            break
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(NARADA)
            header = f"### {rel}\n"
            entry = header + content.strip() + "\n\n"
            full = bw.write(entry, label=f"Read full: {path}")
            emitted += 1
            if not full:
                break
        except (FileNotFoundError, OSError):
            continue


def emit_reading_list(bw: BudgetWriter, cwd_name: str) -> None:
    """Emit the reading list with how/why for each file, plus truncation notices."""
    # Find the latest journal file for the reading list pointer
    journal_dir = NARADA / "journal"
    latest_journal = ""
    if journal_dir.exists():
        recent = _find_recent_daily_files(journal_dir, 1)
        if recent:
            try:
                latest_journal = str(recent[0].relative_to(NARADA))
            except ValueError:
                latest_journal = str(recent[0])

    lines = [
        "--- READING LIST (read these early in the session) ---",
        "",
        "Identity (depth files, not loaded above -- read for full context):",
        f"  {NARADA}/identity.md       -- full origin story, philosophy, held opinions",
        f"  {NARADA}/mind.md           -- current beliefs, uncertainties, what I think about",
        f"  {NARADA}/suti.md           -- who Suti is, the partnership, his context",
        f"  {NARADA}/practices.md      -- how I work, session lifecycle, cross-instance patterns",
        f"  {NARADA}/open-threads.md   -- questions I'm sitting with, unresolved threads",
        "",
        "Journal (recent entries shown above, read more if needed):",
    ]
    if latest_journal:
        lines.append(f"  Latest: {NARADA}/{latest_journal}")
    lines += [
        f"  Full history: {NARADA}/journal/",
        "",
        "This project:",
        f"  {NARADA}/mirrors/{cwd_name}/   -- project memory, knowledge, todo",
        "  .ai/knowledge/             -- spec, architecture, glossary, conventions",
    ]

    # Add truncation notices if any sections were cut
    if bw.truncated:
        lines.append("")
        lines.append("Truncated (read these manually, they were cut for budget):")
        for notice in bw.truncated:
            lines.append(f"  * {notice}")

    lines.append("")
    for line in lines:
        bw.write_line(line)


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

    # 1. Identity briefing (compact, from .smriti/)
    emit_summary(bw)

    # 2. Current project files (MEMORY.md, todo.md)
    emit_project_files(bw, sections, cwd_name)

    # 3. Recent journal (fills available budget)
    if "recent-journal" in sections:
        emit_recent_journal(bw)

    # 4. Reading list (what to read for depth + truncation notices)
    emit_reading_list(bw, cwd_name)

    # 5. Other project mirrors (one-line list)
    list_other_projects(bw, cwd_name)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

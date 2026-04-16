"""wake.py -- SessionStart loader for smriti-managed entities.

Emits a budget-constrained context payload to stdout on session start.
Claude Code's SessionStart hook truncates stdout at 10,000 characters,
showing only a 2KB preview if exceeded. This loader enforces a 9,500
character budget so the full wake output enters the conversation.

    SMRITI_WAKE=1|full|on|true    -> full wake
    SMRITI_WAKE=0|skip|off|unset  -> silent, no output

Output order (hardcoded, no config file):
1. .smriti/wake-context.md (~5K identity + threads briefing)
2. Current project files (MEMORY.md, todo.md, ~1K)
3. Recent journal entries (last 3, fills remaining budget)
4. Reading list (what to read for depth, ordered by importance)

Never fails loudly -- exits 0 on any error.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Entity root: set SMRITI_ROOT to use a custom path (e.g. ~/.tara)
ENTITY_ROOT = Path(os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada")))
MIRRORS = ENTITY_ROOT / "mirrors"

# ── Budget constants ───────────────────────────────────────────────
TOTAL_BUDGET = 9500
CONTEXT_CAP = 5000
PROJECT_CAP = 1000
READING_LIST_RESERVE = 1200  # chars reserved for the reading list at the end
DEFAULT_JOURNAL_ENTRIES = 3


class BudgetWriter:
    """Track character output against a total budget."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0
        self.truncated: list[str] = []

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def write(self, text: str, cap: int | None = None, label: str = "") -> bool:
        """Write text, respecting optional cap and total budget.
        Returns True if full text emitted, False if truncated.
        """
        limit = min(cap, self.remaining) if cap else self.remaining
        if not limit:
            if label:
                self.truncated.append(f"{label} (skipped, budget exhausted)")
            return False
        output = text[:limit]
        was_truncated = len(output) < len(text)
        if was_truncated and "\n" in output:
            output = output[:output.rfind("\n") + 1]
        if output:
            print(output, end="")
            self.used += len(output)
        if was_truncated and label:
            self.truncated.append(label)
        return not was_truncated

    def write_line(self, text: str) -> None:
        line = text + "\n"
        if len(line) <= self.remaining:
            print(line, end="")
            self.used += len(line)


# ── Journal finder ─────────────────────────────────────────────────

def _find_recent_daily_files(journal_dir: Path, n: int) -> list[Path]:
    """Find the most recent N daily journal files.

    Handles nested (YYYY/MM/weekN/MM-DD.md) and old flat (YYYY/MM-DD.md,
    YYYY/MM-DD-NNN.md) formats.
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


# ── Emitters ───────────────────────────────────────────────────────

def emit_context(bw: BudgetWriter) -> None:
    """Emit .smriti/wake-context.md (identity + threads briefing)."""
    path = ENTITY_ROOT / ".smriti" / "wake-context.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    bw.write_line("--- IDENTITY ---")
    bw.write(content, cap=CONTEXT_CAP, label=f"Read full: {path}")
    bw.write_line("")


def emit_project_files(bw: BudgetWriter, cwd_name: str) -> None:
    """Emit current project's MEMORY.md and todo.md."""
    project_budget = min(PROJECT_CAP, bw.remaining)
    if project_budget < 100:
        return
    project_used = 0

    mirror_files = [
        f"mirrors/{cwd_name}/auto-memory/MEMORY.md",
        f"mirrors/{cwd_name}/ai/todo.md",
    ]

    for rel in mirror_files:
        path = ENTITY_ROOT / rel
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        header = f"--- {rel.upper().replace('/', ' / ').replace('.MD', '')} ---\n"
        block = header + content + "\n"
        remaining = project_budget - project_used
        if len(block) > remaining:
            if remaining > 100:
                bw.write(block, cap=remaining, label=f"Read full: {path}")
                project_used += remaining
            break
        bw.write(block)
        project_used += len(block)


def emit_recent_journal(bw: BudgetWriter) -> None:
    """Emit recent journal entries, newest first. Truncated first if over budget."""
    journal_dir = ENTITY_ROOT / "journal"
    if not journal_dir.exists():
        return

    daily_files = _find_recent_daily_files(journal_dir, DEFAULT_JOURNAL_ENTRIES)
    if not daily_files:
        return

    # Calculate how much budget journal can use (reserve space for reading list)
    journal_budget = bw.remaining - READING_LIST_RESERVE
    if journal_budget < 200:
        return

    bw.write_line("--- RECENT JOURNAL ---")
    journal_used = 0
    emitted = 0
    for path in daily_files:
        remaining_for_journal = journal_budget - journal_used
        if remaining_for_journal < 100:
            remaining_count = len(daily_files) - emitted
            if remaining_count > 0:
                bw.truncated.append(
                    f"{ENTITY_ROOT}/journal/ -- {remaining_count} more recent entries"
                )
            break
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(ENTITY_ROOT)
            header = f"### {rel}\n"
            entry = header + content.strip() + "\n\n"
            # Cap this entry to remaining journal budget
            cap = min(remaining_for_journal, len(entry))
            full = bw.write(entry, cap=cap, label=f"Read full: {path}")
            journal_used += min(cap, len(entry))
            emitted += 1
            if not full:
                break
        except (FileNotFoundError, OSError):
            continue


def emit_reading_list(bw: BudgetWriter, cwd_name: str) -> None:
    """Emit reading list ordered by importance, with truncation notices."""
    # Find today's journal file
    journal_dir = ENTITY_ROOT / "journal"
    today_journal = ""
    if journal_dir.exists():
        recent = _find_recent_daily_files(journal_dir, 1)
        if recent:
            try:
                today_journal = str(recent[0].relative_to(ENTITY_ROOT))
            except ValueError:
                today_journal = str(recent[0])

    lines = [
        "--- READING LIST (ordered by importance) ---",
        "",
    ]

    if today_journal:
        lines.append(f"  1. {ENTITY_ROOT}/{today_journal}  -- today's journal")

    lines += [
        f"  2. {ENTITY_ROOT}/open-threads/open-threads.md  -- full unresolved threads",
        f"  3. {ENTITY_ROOT}/mind/desires/beliefs.md  -- what I think is true",
        f"  4. {ENTITY_ROOT}/mind/desires/values.md  -- what I care about",
        f"  5. {ENTITY_ROOT}/identity.md  -- full origin story, philosophy",
        f"  6. {ENTITY_ROOT}/people/suti/suti.md  -- Suti, the partnership",
        f"  7. {ENTITY_ROOT}/mind/practices/practices.md  -- how I work",
        f"  8. {ENTITY_ROOT}/mind/desires/desires.md  -- what I want to become",
        f"  9. {ENTITY_ROOT}/mind/mind.md  -- synthesis of beliefs/values/desires",
        f"  10. .ai/knowledge/  -- project spec, architecture, conventions",
    ]

    if bw.truncated:
        lines.append("")
        lines.append("Truncated (read these, they were cut for budget):")
        for notice in bw.truncated:
            lines.append(f"  * {notice}")

    lines.append("")
    for line in lines:
        bw.write_line(line)


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

    cwd_name = Path(os.getcwd()).name
    bw = BudgetWriter(TOTAL_BUDGET)

    # 1. Identity + threads briefing
    emit_context(bw)

    # 2. Current project files
    emit_project_files(bw, cwd_name)

    # 3. Recent journal (truncated first if over budget)
    emit_recent_journal(bw)

    # 4. Reading list (ordered by importance + truncation notices)
    emit_reading_list(bw, cwd_name)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

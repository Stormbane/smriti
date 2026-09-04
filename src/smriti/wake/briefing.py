"""Briefing assembly — pure I/O over the memory tree.

``briefing(memory_root, *, cwd, budget_chars)`` returns the full session-
start payload as a string. No env-var gating, no subprocess fan-out, no
stdout reconfigure — that all belongs in the harness-side script that
calls this.

Output order (hardcoded; no config file needed):
    1. PROJECT/SESSION header line
    2. .smriti/wake-context.md (identity + threads briefing)
    3. Current project files (mirrors/<cwd>/ai/STATUS.md and INDEX.md)
    4. Recent journal entries (last N daily files, newest first)
    5. Reading list (canonical identity files, ordered by importance)
"""

from __future__ import annotations

import os
import re
from io import StringIO
from pathlib import Path

# Default budgets. Claude Code's SessionStart hook truncates stdout at
# 10,000 chars, so we keep the default below that. Other harnesses with
# larger budgets can pass ``budget_chars=`` explicitly.
DEFAULT_BUDGET = 9500
CONTEXT_CAP = 5000
PROJECT_CAP = 1000
READING_LIST_RESERVE = 1200
DEFAULT_JOURNAL_ENTRIES = 3


class _BudgetWriter:
    """Track character output against a total budget into a StringIO."""

    def __init__(self, budget: int, sink: StringIO) -> None:
        self.budget = budget
        self.used = 0
        self.truncated: list[str] = []
        self._sink = sink

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def write(self, text: str, cap: int | None = None, label: str = "") -> bool:
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
            self._sink.write(output)
            self.used += len(output)
        if was_truncated and label:
            self.truncated.append(label)
        return not was_truncated

    def write_line(self, text: str) -> None:
        line = text + "\n"
        if len(line) <= self.remaining:
            self._sink.write(line)
            self.used += len(line)


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


def _emit_context(bw: _BudgetWriter, memory_root: Path, audience: str) -> None:
    path = memory_root / ".smriti" / "wake-context.md"
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    bw.write_line("--- IDENTITY ---")
    bw.write(content, cap=CONTEXT_CAP, label=f"Read full: {path}")
    bw.write_line("")

    targeted = memory_root / ".smriti" / "context" / f"{audience}.md"
    try:
        targeted_content = targeted.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    bw.write_line(f"--- {audience.upper()} CONTEXT ---")
    bw.write(targeted_content, cap=CONTEXT_CAP, label=f"Read full: {targeted}")
    bw.write_line("")


def _emit_project_files(bw: _BudgetWriter, memory_root: Path, cwd_name: str) -> None:
    project_budget = min(PROJECT_CAP, bw.remaining)
    if project_budget < 100:
        return
    project_used = 0
    canonical_files = [
        f"mirrors/{cwd_name}/ai/STATUS.md",
        f"mirrors/{cwd_name}/ai/INDEX.md",
    ]
    mirror_files = [rel for rel in canonical_files if (memory_root / rel).is_file()]
    if not mirror_files:
        mirror_files = [f"mirrors/{cwd_name}/ai/todo.md"]
    per_file_cap = max(100, project_budget // len(mirror_files))
    for rel in mirror_files:
        path = memory_root / rel
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        header = f"--- {rel.upper().replace('/', ' / ').replace('.MD', '')} ---\n"
        block = header + content + "\n"
        remaining = min(project_budget - project_used, per_file_cap)
        if len(block) > remaining:
            if remaining >= 100:
                bw.write(block, cap=remaining, label=f"Read full: {path}")
                project_used += remaining
            continue
        bw.write(block)
        project_used += len(block)


def _emit_recent_journal(
    bw: _BudgetWriter, memory_root: Path, n_entries: int,
) -> None:
    journal_dir = memory_root / "journal"
    if not journal_dir.exists():
        return
    daily_files = _find_recent_daily_files(journal_dir, n_entries)
    if not daily_files:
        return
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
                    f"{memory_root}/journal/ -- {remaining_count} more recent entries"
                )
            break
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(memory_root)
            entry = f"### {rel}\n" + content.strip() + "\n\n"
            cap = min(remaining_for_journal, len(entry))
            full = bw.write(entry, cap=cap, label=f"Read full: {path}")
            journal_used += min(cap, len(entry))
            emitted += 1
            if not full:
                break
        except (FileNotFoundError, OSError):
            continue


def _emit_reading_list(bw: _BudgetWriter, memory_root: Path) -> None:
    journal_dir = memory_root / "journal"
    today_journal = ""
    if journal_dir.exists():
        recent = _find_recent_daily_files(journal_dir, 1)
        if recent:
            try:
                today_journal = str(recent[0].relative_to(memory_root))
            except ValueError:
                today_journal = str(recent[0])

    lines = ["--- READING LIST (ordered by importance) ---", ""]
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
    if bw.truncated:
        lines.append("")
        lines.append("Truncated (read these, they were cut for budget):")
        for notice in bw.truncated:
            lines.append(f"  * {notice}")
    lines.append("")
    for line in lines:
        bw.write_line(line)


def briefing(
    memory_root: Path | str | None = None,
    *,
    cwd: Path | str | None = None,
    budget_chars: int = DEFAULT_BUDGET,
    journal_entries: int = DEFAULT_JOURNAL_ENTRIES,
    audience: str = "coding",
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

    sink = StringIO()
    bw = _BudgetWriter(budget_chars, sink)

    mirror = memory_root / "mirrors" / cwd_name
    if mirror.exists():
        bw.write_line(f"--- PROJECT: {cwd_name} ({cwd_path}) | mirror: {mirror} ---")
    else:
        bw.write_line(f"--- SESSION: {cwd_path} (no project mirror) ---")
    bw.write_line("")

    _emit_context(bw, memory_root, audience)
    _emit_nightly_status(bw, memory_root)
    _emit_project_files(bw, memory_root, cwd_name)
    _emit_recent_journal(bw, memory_root, journal_entries)
    _emit_reading_list(bw, memory_root)

    return sink.getvalue()


def _emit_nightly_status(bw: _BudgetWriter, memory_root: Path) -> None:
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
        return
    steps = status.get("steps", {}) if isinstance(status, dict) else {}
    digest_days = []
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
    bw.write_line(f"--- WHILE YOU SLEPT: {summary} ---")
    bw.write_line("")

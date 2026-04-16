"""wake.py -- SessionStart loader for smriti-managed entities.

Reads <entity-root>/wake.md, resolves {project} from cwd basename, emits
the requested files to stdout. Claude Code's SessionStart hook forwards
hook stdout to the assistant as session context.

**Default is silent.** Wake only emits if `SMRITI_WAKE` is set to a truthy
value -- which keeps `claude -p` and other non-interactive callers clean.
Interactive sessions set `SMRITI_WAKE=1` in their SessionStart hook wiring.

    SMRITI_WAKE=1|full|on|true    -> full wake (identity + project + mirrors)
    SMRITI_WAKE=0|skip|off|unset  -> silent, no output

Behavior when full wake fires:
- Always-load section: unconditional reads.
- Recent-journal section: emits the last N daily journal files for
  recent-self continuity (default: 3 days).
- Current-project section: {project} substituted with basename(cwd).
- Missing files: skipped silently (no file emitted, no error).
- Lists other project mirrors so the entity knows what cross-project
  memory is reachable on demand.

Never fails loudly -- any error exits 0 with a stderr note. A hook failure
must not block the session from starting.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

NARADA = Path.home() / ".narada"
WAKE = NARADA / "wake.md"
MIRRORS = NARADA / "mirrors"

DEFAULT_JOURNAL_DAYS = 3


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


def emit_file(rel: str, header: str) -> None:
    path = NARADA / rel
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    print(f"--- {header} ---")
    print(content)
    print()


def emit_recent_journal(n: int = DEFAULT_JOURNAL_DAYS) -> None:
    """Find and emit the most recent N daily journal files.

    Scans all branch directories for YYYY/MM-DD.md files (the daily
    format used by smriti's writer), sorts by date descending, and
    emits the most recent N. This gives the entity continuity with
    its recent self -- what it was thinking, deciding, noticing.
    """
    journal_dir = NARADA / "journal"
    if not journal_dir.exists():
        return

    # Collect all daily files across year directories
    daily_files: list[tuple[str, Path]] = []
    for year_dir in sorted(journal_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for md_file in sorted(year_dir.glob("*.md"), reverse=True):
            if md_file.name == "index.md":
                continue
            # Sort key is YYYY/MM-DD (works lexicographically)
            sort_key = f"{year_dir.name}/{md_file.stem}"
            daily_files.append((sort_key, md_file))
            if len(daily_files) >= n:
                break
        if len(daily_files) >= n:
            break

    if not daily_files:
        return

    # Emit most recent first
    daily_files.sort(reverse=True)
    print(f"--- RECENT JOURNAL (last {len(daily_files)} days) ---")
    for _key, path in daily_files[:n]:
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(NARADA)
            print(f"### {rel}")
            print(content.strip())
            print()
        except (FileNotFoundError, OSError):
            continue
    print()


def list_other_projects(current: str) -> None:
    if not MIRRORS.exists():
        return
    others = sorted(
        p.name for p in MIRRORS.iterdir()
        if p.is_dir() and p.name != current
    )
    if not others:
        return
    print("--- OTHER PROJECT MIRRORS (available on demand) ---")
    print(
        "Other projects whose memory you can read via "
        "~/.narada/mirrors/<name>/ :"
    )
    for name in others:
        print(f"  - {name}")
    print()


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

    for rel in sections.get("always", []):
        emit_file(rel, rel.upper().replace("/", " / ").replace(".MD", ""))

    # Recent journal: emit last N daily journal files for continuity
    if "recent-journal" in sections:
        emit_recent_journal()

    for raw in sections.get("current-project", []):
        rel = raw.replace("{project}", cwd_name)
        header = rel.upper().replace("/", " / ").replace(".MD", "")
        emit_file(rel, header)

    list_other_projects(cwd_name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

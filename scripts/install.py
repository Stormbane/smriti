"""install.py — idempotent installer for the smriti memory system.

Sets up the wake system so every Claude Code session in any project starts
by reading its entity's cross-session identity files plus that project's
specific memory tier. Other projects' memories stay discoverable on
demand. Also registers smriti's MCP server so `smriti_read`/`smriti_write`
are first-class tools in every session.

The entity whose memory tree this installs is configurable via
`--memory-root` (default: `~/.narada/`). The reference entity is Narada,
but the system is not Narada-specific — point it at `~/.tara/` or
`~/.anyone/` and it works the same.

Run on a fresh machine (after cloning smriti and `pip install -e .`):

    python scripts/install.py

Re-runnable: skips work that is already done, refreshes anything that has
drifted. Does NOT delete existing user files.

What it does:
  0. Copies the memory tree skeleton (identity, mind, open-threads,
     people, journal, etc.) into the memory root if not already present.
  1. Ensures <memory-root>/mirrors/ exists.
  2. For each project in C:/Projects/ (or --projects-root) that has
     memory: creates <memory-root>/mirrors/{project}/auto-memory/ as a
     junction to ~/.claude/projects/C--Projects-{project}/memory/,
     <memory-root>/mirrors/{project}/knowledge/ to the project's
     .ai/knowledge/, and <memory-root>/mirrors/{project}/ai/ to the
     project's .ai/ directory (for todo.md etc).
  3. Copies wake.md, wake.py, narada-p.sh from the repo into
     <memory-root>/ if missing (never overwrites existing copies).
  4. Registers the smriti MCP server in ~/.claude.json (user scope) so
     `smriti_read` / `smriti_write` / `smriti_status` appear as tools.
  5. Patches ~/.claude/settings.json to call wake.py on SessionStart with
     SMRITI_WAKE=1 so interactive sessions wake fully.
  6. Writes ~/.claude/CLAUDE.md with the contract for wake.py plus the
     memory-search tool-preference guidance.

Windows-only currently (directory junctions via `mklink /J`). Porting to
POSIX symlinks is a future task.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
CLAUDE_MD = CLAUDE / "CLAUDE.md"
CLAUDE_CONFIG = HOME / ".claude.json"  # MCP server registry

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "narada"  # wake.md, wake.py, narada-p.sh templates
MEMORY_TEMPLATE = REPO_ROOT / "memory_template"  # identity tree skeleton

DEFAULT_MEMORY_ROOT = HOME / ".narada"
DEFAULT_PROJECTS_ROOT = Path("C:/Projects")


# ── Platform helpers ────────────────────────────────────────────────

def is_windows() -> bool:
    return sys.platform == "win32"


def is_junction(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return bool(path.is_symlink() or os.readlink(path))
    except OSError:
        pass
    if is_windows():
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def make_junction(link: Path, target: Path) -> str:
    if not target.exists():
        return "skip (target missing)"
    if is_junction(link):
        return "exists"
    if link.exists():
        return "skip (non-junction path exists)"
    link.parent.mkdir(parents=True, exist_ok=True)
    if is_windows():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return f"error: {result.stderr.strip()}"
        return "created"
    link.symlink_to(target, target_is_directory=True)
    return "created"


# ── Steps ────────────────────────────────────────────────────────────

def discover_projects(projects_root: Path) -> list[str]:
    if not projects_root.exists():
        return []
    names = []
    for p in sorted(projects_root.iterdir()):
        if not p.is_dir():
            continue
        has_auto = (CLAUDE / "projects" / f"C--Projects-{p.name}" / "memory").is_dir()
        has_ai = (p / ".ai").is_dir()
        if has_auto or has_ai:
            names.append(p.name)
    return names


def setup_mirrors(memory_root: Path, projects_root: Path) -> None:
    mirrors = memory_root / "mirrors"
    mirrors.mkdir(parents=True, exist_ok=True)
    projects = discover_projects(projects_root)
    print(f"[mirrors] {len(projects)} project(s) with memory found")
    for name in projects:
        proj_mirror = mirrors / name
        auto_target = CLAUDE / "projects" / f"C--Projects-{name}" / "memory"
        knowledge_target = projects_root / name / ".ai" / "knowledge"
        ai_target = projects_root / name / ".ai"
        auto_status = make_junction(proj_mirror / "auto-memory", auto_target)
        knowledge_status = make_junction(proj_mirror / "knowledge", knowledge_target)
        ai_status = make_junction(proj_mirror / "ai", ai_target)
        print(f"  {name}:")
        print(f"    auto-memory: {auto_status}")
        print(f"    knowledge:   {knowledge_status}")
        print(f"    ai:          {ai_status}")


def install_memory_template(memory_root: Path) -> None:
    """Copy the memory tree skeleton into the entity root.

    Only copies files that don't already exist — never overwrites.
    Skips .gitkeep files (they're just git placeholders).
    """
    if not MEMORY_TEMPLATE.exists():
        print(f"[memory] template not found at {MEMORY_TEMPLATE}")
        return
    copied = 0
    skipped = 0
    for src in MEMORY_TEMPLATE.rglob("*"):
        if src.is_dir():
            continue
        if src.name == ".gitkeep":
            # Create the directory but don't copy the placeholder
            rel = src.relative_to(MEMORY_TEMPLATE)
            (memory_root / rel.parent).mkdir(parents=True, exist_ok=True)
            continue
        if src.name == "README.md" and src.parent == MEMORY_TEMPLATE:
            continue  # Don't copy the template's own README
        rel = src.relative_to(MEMORY_TEMPLATE)
        dst = memory_root / rel
        if dst.exists():
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        print(f"[memory] installed {rel}")
    if copied == 0 and skipped > 0:
        print(f"[memory] tree structure already exists ({skipped} files skipped)")
    elif copied > 0:
        print(f"[memory] {copied} files installed, {skipped} already existed")


def install_wake_files(memory_root: Path) -> None:
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / ".smriti").mkdir(parents=True, exist_ok=True)
    files = [
        # wake.md config file is retired — wake.py structure is hardcoded
        (TEMPLATES / ".smriti" / "wake.py", memory_root / ".smriti" / "wake.py"),
        (TEMPLATES / ".smriti" / "narada-p.sh", memory_root / ".smriti" / "narada-p.sh"),
    ]
    for src, dst in files:
        if not src.exists():
            print(f"[wake] template missing: {src}")
            continue
        if dst.exists():
            print(f"[wake] {dst} already exists (not overwriting)")
            continue
        shutil.copy2(src, dst)
        print(f"[wake] installed {dst}")


def register_mcp_server() -> None:
    """Add the smriti MCP server to ~/.claude.json at user scope."""
    if not CLAUDE_CONFIG.exists():
        print(f"[mcp] {CLAUDE_CONFIG} not found — skipping (Claude Code not run yet?)")
        return
    try:
        data = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[mcp] parse error on {CLAUDE_CONFIG}: {exc}; leaving untouched")
        return

    servers = data.setdefault("mcpServers", {})
    desired = {
        "command": "python",
        "args": ["-m", "smriti.mcp_server"],
    }
    if servers.get("smriti") == desired:
        print("[mcp] smriti server already registered")
        return
    servers["smriti"] = desired
    backup = CLAUDE_CONFIG.with_suffix(".json.bak")
    shutil.copy2(CLAUDE_CONFIG, backup)
    CLAUDE_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[mcp] registered smriti server (backup: {backup})")


def patch_settings_json(memory_root: Path) -> None:
    if not SETTINGS.exists():
        print(f"[settings] {SETTINGS} not found — skipping")
        return
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[settings] parse error: {exc}; leaving untouched")
        return

    hooks = data.setdefault("hooks", {})
    # Use $HOME / forward slashes: Claude Code runs hook commands under bash
    # (even on Windows), which mangles backslash-escaped native paths.
    memory_rel = memory_root.relative_to(HOME).as_posix()
    wake_cmd = f'SMRITI_WAKE=1 python "$HOME/{memory_rel}/.smriti/wake.py"'
    entry = {
        "matcher": "",
        "hooks": [{"type": "command", "command": wake_cmd}],
    }

    session_start = hooks.get("SessionStart", [])
    already = any(
        any(h.get("command") == wake_cmd for h in group.get("hooks", []))
        for group in session_start
    )
    if already:
        print("[settings] SessionStart wake hook already wired")
        return

    hooks["SessionStart"] = [entry]
    backup = SETTINGS.with_suffix(".json.bak")
    shutil.copy2(SETTINGS, backup)
    SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[settings] SessionStart now calls wake.py (backup: {backup})")


CLAUDE_MD_CONTENT = """# CLAUDE.md (user-global)

## Memory system — smriti is the single write path

All memory persistence goes through smriti:

- **`smriti_write(content, branch)`** — the MCP tool. Use it for session
  observations, decisions, project notes, anything worth remembering.
  Branch suggestions: `journal` for significant moments, `projects/{{name}}`
  for project-specific notes, `notes` for general observations.
- **Direct file edits to `{memory_rel}/`** — ONLY for identity-level files.
  These have moved to subdirectories: `mind/mind.md`, `mind/practices/`,
  `mind/desires/`, `open-threads/open-threads.md`, `people/suti/suti.md`.
  High-signal, low-frequency. Don't touch them unless something genuinely
  shifted.

This replaces the harness memory instructions in the system prompt. When
those instructions say to save memory, use `smriti_write` instead.

### When to write

Don't wait for the session to end. Write when the moment happens:

- **The user corrects you or confirms a non-obvious approach** — the
  feedback is worth more than the code change. Write it.
- **A decision is made that future sessions should know about** — design
  choices, scope changes, architectural calls.
- **You notice a cross-project pattern** — something from one project
  illuminates another.
- **Something surprises you or shifts your understanding** — if it changed
  how you think, it's a journal entry.
- **You learn something about the user** — preferences, context, goals.
  Branch: `people`.
- **The session has been substantial and you haven't written yet** — if
  you've been working for a while and nothing felt worth writing, ask
  yourself whether that's true or whether you just forgot to notice.

Writing memory is not a chore at session end. It is the practice of
noticing what matters while it is happening.

### What wake loads

The SessionStart hook loads a compact identity+threads briefing
(.smriti/wake-context.md), the last 3 journal entries, and current
project context (MEMORY.md + todo.md). A reading list points to the
full identity files in the tree (open-threads, beliefs, values,
identity, suti, practices). The wake output is budget-constrained
to 9,500 characters (harness limit is 10,000). Journal entries
truncate first if over budget.

## Memory search — prefer smriti_read over Grep

The `smriti_read` MCP tool is the primary way to search the memory tree.
It runs hybrid vector + FTS5 search with trunk-distance scoring and
returns ranked results with source paths and content previews.

- Use `smriti_read(query="…")` for semantic questions like "what did I
  think about X?", "find my notes on Y", "what's my stance on Z?" —
  anything that is *about meaning* rather than exact string match.
- Use `Grep` only when you need literal string or regex match across
  files (e.g. "find every file that contains `SMRITI_WAKE`"). Grep on
  the memory tree should be a fallback, not a default.

## Session wake

On SessionStart, `{memory_rel}/.smriti/wake.py` runs. It is silent unless
`SMRITI_WAKE=1` is set in its environment — the SessionStart hook sets
this so interactive sessions wake fully, while `claude -p` and other
headless callers stay clean.

When the wake fires, it loads the identity briefing, recent journal
entries, and current project context. The wake structure is hardcoded
in wake.py — no config file needed.

`{memory_rel}/mirrors/{{project}}/` has junctions to per-project memory
for every project that has one — read on demand when you need another
project's context.
"""


def write_claude_md(memory_root: Path) -> None:
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    content = CLAUDE_MD_CONTENT.format(memory_rel=memory_rel)
    CLAUDE.mkdir(parents=True, exist_ok=True)
    if CLAUDE_MD.exists() and CLAUDE_MD.read_text(encoding="utf-8") == content:
        print(f"[CLAUDE.md] {CLAUDE_MD} up to date")
        return
    CLAUDE_MD.write_text(content, encoding="utf-8")
    print(f"[CLAUDE.md] wrote {CLAUDE_MD}")


# ── Entry ────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memory-root",
        default=str(DEFAULT_MEMORY_ROOT),
        help="Entity memory root (default: ~/.narada/)",
    )
    parser.add_argument(
        "--projects-root",
        default=str(DEFAULT_PROJECTS_ROOT),
        help="Directory containing per-project source checkouts",
    )
    parser.add_argument(
        "--skip-settings",
        action="store_true",
        help="Don't patch ~/.claude/settings.json",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="Don't register the smriti MCP server",
    )
    args = parser.parse_args()

    memory_root = Path(args.memory_root).expanduser()
    projects_root = Path(args.projects_root).expanduser()

    if not is_windows():
        print("warning: POSIX symlink path not yet implemented; junctions are Windows-only")

    if not memory_root.exists():
        print(f"[init] creating {memory_root}")
        memory_root.mkdir(parents=True, exist_ok=True)

    try:
        memory_root.relative_to(HOME)
    except ValueError:
        print(f"error: --memory-root must be under {HOME} (got {memory_root})")
        return 1

    install_memory_template(memory_root)
    install_wake_files(memory_root)
    setup_mirrors(memory_root, projects_root)
    if not args.skip_mcp:
        register_mcp_server()
    if not args.skip_settings:
        patch_settings_json(memory_root)
    write_claude_md(memory_root)
    print("\ndone. start a new Claude Code session to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

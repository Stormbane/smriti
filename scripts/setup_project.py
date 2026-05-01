"""setup_project.py -- Set up a project to use smriti for memory.

Run from the project directory, or pass the project path as an argument:

    cd C:/Projects/my-thing
    python C:/Projects/smriti/scripts/setup_project.py
    python scripts/setup_project.py C:/Projects/my-thing            # default: both harnesses
    python scripts/setup_project.py --harness codex                 # Codex only
    python scripts/setup_project.py --harness claude_code           # Claude Code only

What it does:
  1. Copies the project template (.ai/ skeleton + per-harness agent doc)
     for the selected harness(es).
  2. Ensures .claude/ and .ai/ are in .gitignore.
  3. Creates mirror junctions in <memory-root>/mirrors/{project-name}/.
     The Claude-Code-specific auto-memory junction is only attempted when
     `claude_code` is in the harness selection AND ~/.claude/ exists.
  4. Cleans up stale junctions from old setups.

Use --no-template to skip the template copy (only create mirrors).
Use --memory-root to target a different entity (default: ~/.narada).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "project_template"

KNOWN_HARNESSES = ("claude_code", "codex", "both")

# Lines that must be in .gitignore for any smriti-managed project.
GITIGNORE_REQUIRED = [
    "# Claude Code internal",
    ".claude/",
]


def _is_junction(path: Path) -> bool:
    """Reliable junction-or-symlink check. ``Path.is_junction`` doesn't
    exist on Python 3.11 stdlib; rely on the reparse-point bit on Windows
    plus the cross-platform symlink check."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            FILE_ATTRIBUTE_REPARSE_POINT = 0x400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False
    return False


def mangle_path(project_path: Path) -> str:
    """Convert a project path to Claude Code's harness directory name.

    C:/Projects/my-app -> C--Projects-my-app
    Both ':' and path separators become '-'.
    """
    return str(project_path).replace(":", "-").replace("\\", "-").replace("/", "-")


def make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction (link -> target)."""
    if link.exists():
        try:
            existing = os.readlink(link)
        except OSError:
            existing = "(unreadable)"
        print(f"  exists: {link.name} -> {existing}")
        return True
    if not target.exists():
        print(f"  skip:   {link.name} (target does not exist: {target})")
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  linked: {link.name} -> {target}")
            return True
        print(f"  FAILED: {link.name} -> {target}")
        print(f"          {result.stderr.strip()}")
        return False
    # POSIX symlink fallback (Linux/macOS — symlinks behave like junctions).
    try:
        link.symlink_to(target, target_is_directory=True)
        print(f"  linked: {link.name} -> {target}")
        return True
    except OSError as exc:
        print(f"  FAILED: {link.name} -> {target}: {exc}")
        return False


def clean_stale_junctions(mirror: Path) -> None:
    """Remove junctions whose targets no longer exist."""
    if not mirror.exists():
        return
    for entry in mirror.iterdir():
        if not _is_junction(entry):
            continue
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if Path(target).exists():
            continue
        if sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "rmdir", str(entry)],
                capture_output=True, text=True,
            )
        else:
            try:
                entry.unlink()
            except OSError:
                continue
        print(f"  cleaned stale junction: {entry.name} -> {target}")


def _harness_doc_files(harness: str) -> list[str]:
    """Which agent-doc files to copy from the template for a harness."""
    if harness == "claude_code":
        return ["CLAUDE.md"]
    if harness == "codex":
        return ["AGENTS.md"]
    if harness == "both":
        return ["CLAUDE.md", "AGENTS.md"]
    raise ValueError(f"unknown harness {harness!r}")


def copy_template(project: Path, harness: str) -> None:
    """Copy template files into the project, skipping files that already exist.

    Per-harness agent docs (CLAUDE.md, AGENTS.md) are filtered by the
    selected harness. Everything else (the .ai/ skeleton, etc.) is copied
    unconditionally because it's harness-neutral.
    """
    if not TEMPLATE.exists():
        print(f"[template] not found at {TEMPLATE}")
        return
    wanted_docs = set(_harness_doc_files(harness))
    all_known_docs = {"CLAUDE.md", "AGENTS.md"}

    copied = 0
    skipped = 0
    for src_path in TEMPLATE.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(TEMPLATE)
        # .gitignore is handled separately via ensure_gitignore
        if rel.name == ".gitignore":
            continue
        # Skip per-harness docs that weren't selected.
        if rel.name in all_known_docs and rel.name not in wanted_docs:
            continue
        dst = project / rel
        if dst.exists():
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst)
        print(f"  copied: {rel}")
        copied += 1
    if copied == 0 and skipped > 0:
        print(f"  all {skipped} template files already exist")
    elif copied > 0:
        print(f"  {copied} file(s) copied, {skipped} already existed")


def ensure_gitignore(project: Path) -> None:
    """Ensure .gitignore contains the required lines for smriti projects."""
    gitignore = project / ".gitignore"

    if not gitignore.exists():
        # No .gitignore at all -- copy the template's
        template_gi = TEMPLATE / ".gitignore"
        if template_gi.exists():
            shutil.copy2(template_gi, gitignore)
            print("[gitignore] copied template .gitignore")
        else:
            # Write just the essentials
            gitignore.write_text("\n".join(GITIGNORE_REQUIRED) + "\n", encoding="utf-8")
            print("[gitignore] created with .claude/ ignore")
        return

    content = gitignore.read_text(encoding="utf-8")
    lines_to_add = []
    for line in GITIGNORE_REQUIRED:
        if line.startswith("#"):
            continue
        if line.rstrip("/") not in content and line not in content:
            lines_to_add.append(line)

    if not lines_to_add:
        print("[gitignore] already has required entries")
        return

    # Append missing lines
    if not content.endswith("\n"):
        content += "\n"
    content += "\n# Added by smriti setup\n"
    for line in lines_to_add:
        content += line + "\n"
    gitignore.write_text(content, encoding="utf-8")
    print(f"[gitignore] appended: {', '.join(lines_to_add)}")


def setup_mirror(
    project: Path,
    mirrors: Path,
    *,
    wants_claude_code: bool,
) -> None:
    """Create per-project mirror junctions under <memory-root>/mirrors/.

    Always: knowledge/ and ai/ junctions (harness-neutral content).
    Conditional: auto-memory/ junction (Claude Code only — Codex has no
    equivalent per-project memory dir).
    Always: native subdirs for findings/decisions/research.
    """
    mirror = mirrors / project.name
    mirror.mkdir(parents=True, exist_ok=True)
    print(f"[mirrors] {mirror}")

    clean_stale_junctions(mirror)

    if wants_claude_code:
        if (HOME / ".claude").exists():
            mangled = mangle_path(project)
            harness_memory = CLAUDE_PROJECTS / mangled / "memory"
            harness_memory.mkdir(parents=True, exist_ok=True)
            make_junction(mirror / "auto-memory", harness_memory)
        else:
            print("  skip auto-memory: ~/.claude/ does not exist")

    knowledge = project / ".ai" / "knowledge"
    make_junction(mirror / "knowledge", knowledge)

    ai_dir = project / ".ai"
    make_junction(mirror / "ai", ai_dir)

    # Native directories for smriti-side memory (not junctioned). These hold
    # durable, cascade-aware artifacts that live in the memory tree, not in
    # the project repo:
    #   findings/   -- lessons learned (cascade upward to cross-project)
    #   decisions/  -- ADRs with rationale
    #   research/   -- exploration without commitment
    for sub in ("findings", "decisions", "research"):
        d = mirror / sub
        d.mkdir(parents=True, exist_ok=True)
        idx = d / "index.md"
        if not idx.exists():
            idx.write_text(
                f"# {sub.title()} -- {project.name}\n\n"
                f"_Cascade-maintained index. Updated automatically when "
                f"new entries are added._\n",
                encoding="utf-8",
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up a project to use smriti for memory.",
    )
    parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="Project directory path (default: current directory)",
    )
    parser.add_argument(
        "--memory-root",
        default=str(HOME / ".narada"),
        help="Entity memory root (default: ~/.narada)",
    )
    parser.add_argument(
        "--harness",
        default="both",
        choices=KNOWN_HARNESSES,
        help="Agent harness(es) to wire this project for (default: both)",
    )
    parser.add_argument(
        "--no-template",
        action="store_true",
        help="Skip template copy, only create mirror junctions",
    )
    args = parser.parse_args()

    project = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    memory_root = Path(args.memory_root).expanduser()
    mirrors = memory_root / "mirrors"
    harness = args.harness

    if not project.exists():
        print(f"error: {project} does not exist")
        return 1

    name = project.name
    print(f"Project:      {project}")
    print(f"Name:         {name}")
    print(f"Memory root:  {memory_root}")
    print(f"Harness:      {harness}")
    print()

    # Step 1: Copy template files if needed
    if not args.no_template:
        print("[template]")
        copy_template(project, harness)
        print()

    # Step 2: Ensure .gitignore has required entries
    ensure_gitignore(project)
    print()

    # Step 3: Create mirror junctions
    setup_mirror(
        project,
        mirrors,
        wants_claude_code=harness in ("claude_code", "both"),
    )

    print()
    print("Done. wake.py will load this project's mirror on session start.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

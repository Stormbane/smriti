"""setup_project.py -- Set up a project to use smriti for memory.

Run from the project directory, or pass the project path as an argument:

    cd C:/Projects/my-thing
    python C:/Projects/smriti/scripts/setup_project.py

    # or explicitly:
    python scripts/setup_project.py C:/Projects/my-thing

What it does:
  1. Copies the project template (.ai/ skeleton, CLAUDE.md) if not present
  2. Ensures .claude/ and .ai/ are in .gitignore
  3. Creates mirror junctions in <memory-root>/mirrors/{project-name}/
  4. Cleans up stale junctions from old setups

Use --no-template to skip the template copy (only create mirrors).
Use --memory-root to target a different entity (default: ~/.narada).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "project_template"

# Lines that must be in .gitignore for any smriti-managed project.
GITIGNORE_REQUIRED = [
    "# Claude Code internal",
    ".claude/",
]


def mangle_path(project_path: Path) -> str:
    """Convert a project path to Claude Code's harness directory name.

    C:/Projects/my-app -> C--Projects-my-app
    Both ':' and path separators become '-'.
    """
    return str(project_path).replace(":", "-").replace("\\", "-").replace("/", "-")


def make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction (link -> target)."""
    if link.exists():
        print(f"  exists: {link.name} -> {os.readlink(link)}")
        return True
    if not target.exists():
        print(f"  skip:   {link.name} (target does not exist: {target})")
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  linked: {link.name} -> {target}")
        return True
    else:
        print(f"  FAILED: {link.name} -> {target}")
        print(f"          {result.stderr.strip()}")
        return False


def clean_stale_junctions(mirror: Path) -> None:
    """Remove junctions whose targets no longer exist."""
    if not mirror.exists():
        return
    for entry in mirror.iterdir():
        if entry.is_symlink() or entry.is_junction():
            try:
                target = os.readlink(entry)
                if not Path(target).exists():
                    subprocess.run(
                        ["cmd", "/c", "rmdir", str(entry)],
                        capture_output=True, text=True,
                    )
                    print(f"  cleaned stale junction: {entry.name} -> {target}")
            except OSError:
                pass


def copy_template(project: Path) -> None:
    """Copy template files into the project, skipping files that already exist."""
    if not TEMPLATE.exists():
        print(f"[template] not found at {TEMPLATE}")
        return
    copied = 0
    skipped = 0
    for src_path in TEMPLATE.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(TEMPLATE)
        # .gitignore is handled separately via ensure_gitignore
        if rel.name == ".gitignore":
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
        "--no-template",
        action="store_true",
        help="Skip template copy, only create mirror junctions",
    )
    args = parser.parse_args()

    project = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    memory_root = Path(args.memory_root).expanduser()
    mirrors = memory_root / "mirrors"

    if not project.exists():
        print(f"error: {project} does not exist")
        return 1

    name = project.name
    print(f"Project:      {project}")
    print(f"Name:         {name}")
    print(f"Memory root:  {memory_root}")
    print()

    # Step 1: Copy template files if needed
    if not args.no_template:
        print("[template]")
        copy_template(project)
        print()

    # Step 2: Ensure .gitignore has required entries
    ensure_gitignore(project)
    print()

    # Step 3: Create mirror junctions
    mirror = mirrors / name
    mirror.mkdir(parents=True, exist_ok=True)
    print(f"[mirrors] {mirror}")

    clean_stale_junctions(mirror)

    mangled = mangle_path(project)
    harness_memory = CLAUDE_PROJECTS / mangled / "memory"
    harness_memory.mkdir(parents=True, exist_ok=True)
    make_junction(mirror / "auto-memory", harness_memory)

    knowledge = project / ".ai" / "knowledge"
    make_junction(mirror / "knowledge", knowledge)

    ai_dir = project / ".ai"
    make_junction(mirror / "ai", ai_dir)

    print()
    print("Done. wake.py will now load this project's mirror on session start.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

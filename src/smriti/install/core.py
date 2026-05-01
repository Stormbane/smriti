"""Core install steps — harness-neutral.

These run regardless of which agent harness will eventually drive
smriti. They set up the memory tree, copy wake templates, link
project mirrors, and (optionally) start the qmd recall daemon.

Per-harness install bits (hooks, settings.json, CLAUDE.md, MCP
registration) live in ``smriti.integrations.<harness>.install``.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Repo-relative locations of the templates we copy.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TEMPLATES = _REPO_ROOT / "narada"  # wake.py, backup.py, narada-p.sh
MEMORY_TEMPLATE = _REPO_ROOT / "memory_template"  # identity tree skeleton

HOME = Path.home()
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
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"error: {result.stderr.strip()}"
        return "created"
    link.symlink_to(target, target_is_directory=True)
    return "created"


# ── Steps ────────────────────────────────────────────────────────────


def discover_projects(projects_root: Path, claude_dir: Path | None = None) -> list[str]:
    """Project basenames whose tree has either a Claude-Code auto-memory dir
    or an ``.ai`` directory."""
    if not projects_root.exists():
        return []
    claude_dir = claude_dir or (HOME / ".claude")
    names: list[str] = []
    for p in sorted(projects_root.iterdir()):
        if not p.is_dir():
            continue
        has_auto = (claude_dir / "projects" / f"C--Projects-{p.name}" / "memory").is_dir()
        has_ai = (p / ".ai").is_dir()
        if has_auto or has_ai:
            names.append(p.name)
    return names


def setup_mirrors(
    memory_root: Path,
    projects_root: Path,
    claude_dir: Path | None = None,
) -> None:
    """Junction per-project memory + knowledge into ``<memory_root>/mirrors``.

    The auto-memory leg is Claude-Code-specific — if ``claude_dir`` doesn't
    exist or has no per-project memory dirs, that leg is silently skipped
    and only ``ai/`` and ``knowledge/`` get linked.
    """
    claude_dir = claude_dir or (HOME / ".claude")
    mirrors = memory_root / "mirrors"
    mirrors.mkdir(parents=True, exist_ok=True)
    projects = discover_projects(projects_root, claude_dir=claude_dir)
    print(f"[mirrors] {len(projects)} project(s) with memory found")
    for name in projects:
        proj_mirror = mirrors / name
        auto_target = claude_dir / "projects" / f"C--Projects-{name}" / "memory"
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
    """Copy the identity-tree skeleton into ``memory_root``.

    Never overwrites existing files. Skips ``.gitkeep`` placeholders.
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
            rel = src.relative_to(MEMORY_TEMPLATE)
            (memory_root / rel.parent).mkdir(parents=True, exist_ok=True)
            continue
        if src.name == "README.md" and src.parent == MEMORY_TEMPLATE:
            continue
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
    """Copy wake.py / backup.py / narada-p.sh into ``<memory_root>/.smriti/``.

    Auto-upgrades smriti-managed scripts in place. The detection rule is
    "first 400 bytes mention 'smriti'" — cheap and effective because every
    template version we've ever shipped names the project in its docstring,
    while a genuinely hand-rolled replacement would not. When upgrading we
    drop a ``.pre-upgrade.bak`` next to the file so nothing is lost.

    Hand-customized files (no 'smriti' marker) are left alone with a
    warning. ``narada-p.sh`` and other future templates use the same path.
    """
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / ".smriti").mkdir(parents=True, exist_ok=True)
    files = [
        (TEMPLATES / ".smriti" / "wake.py", memory_root / ".smriti" / "wake.py"),
        (TEMPLATES / ".smriti" / "backup.py", memory_root / ".smriti" / "backup.py"),
        (TEMPLATES / ".smriti" / "narada-p.sh", memory_root / ".smriti" / "narada-p.sh"),
    ]
    for src, dst in files:
        if not src.exists():
            print(f"[wake] template missing: {src}")
            continue
        if not dst.exists():
            shutil.copy2(src, dst)
            print(f"[wake] installed {dst}")
            continue

        # Already exists — decide between leave-alone, up-to-date, upgrade.
        try:
            src_bytes = src.read_bytes()
            dst_bytes = dst.read_bytes()
        except OSError as exc:
            print(f"[wake] read error on {dst}: {exc}; leaving untouched")
            continue

        if src_bytes == dst_bytes:
            print(f"[wake] {dst.name} up to date")
            continue

        # Marker check on the first 400 bytes (covers the docstring of any
        # smriti-managed template we've shipped).
        head = dst_bytes[:400].decode("utf-8", errors="ignore").lower()
        if "smriti" not in head:
            print(
                f"[wake] {dst.name} differs from template but is not "
                "smriti-managed (no 'smriti' marker in head); leaving untouched"
            )
            continue

        backup = dst.with_suffix(dst.suffix + ".pre-upgrade.bak")
        try:
            shutil.copy2(dst, backup)
        except OSError as exc:
            print(f"[wake] backup failed for {dst}: {exc}; leaving untouched")
            continue
        shutil.copy2(src, dst)
        print(f"[wake] upgraded {dst.name} (backup: {backup.name})")


def start_recall_daemon() -> None:
    """Start qmd's HTTP daemon so recall hits the warm path."""
    try:
        from smriti.recall.backends import qmd as qmd_be
    except ImportError:
        print("[recall] smriti.recall not importable; skipping daemon start")
        return
    if not qmd_be.is_available():
        print("[recall] qmd not on PATH; install via `npm install -g @tobilu/qmd` "
              "then run `smriti recall daemon start`")
        return
    if qmd_be.daemon_health(timeout_s=0.5):
        print("[recall] qmd daemon already up")
        return
    ok, msg = qmd_be.daemon_start()
    print(f"[recall] qmd daemon start: {msg}")


# ── Top-level core entry point ──────────────────────────────────────


def run_core(memory_root: Path, projects_root: Path, *, skip_recall_daemon: bool = False) -> None:
    """Run all core install steps. Idempotent."""
    if not is_windows():
        print("warning: POSIX symlink path not yet implemented; junctions are Windows-only")

    if not memory_root.exists():
        print(f"[init] creating {memory_root}")
        memory_root.mkdir(parents=True, exist_ok=True)

    try:
        memory_root.relative_to(HOME)
    except ValueError:
        raise RuntimeError(f"--memory-root must be under {HOME} (got {memory_root})")

    install_memory_template(memory_root)
    install_wake_files(memory_root)
    setup_mirrors(memory_root, projects_root)
    if not skip_recall_daemon:
        start_recall_daemon()

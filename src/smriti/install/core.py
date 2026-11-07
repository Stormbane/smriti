"""Core install steps — harness-neutral.

These run regardless of which agent harness will eventually drive
smriti. They set up the memory tree, copy wake templates, link
project mirrors, and (optionally) start the qmd recall daemon.

The files copied come from :mod:`smriti.install.templates`: the
package's own copies in a wheel, the checkout's otherwise. The projects
root, the directory whose projects are linked into ``mirrors/``, is
recorded in ``<root>/.smriti/install.json`` and reused by the next run
(:func:`resolve_projects_root`).

Per-harness install bits (hooks, settings.json, CLAUDE.md, MCP
registration) live in ``smriti.integrations.<harness>.install``.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smriti.install import templates

# What the installer copies (None when neither the package nor a checkout has it).
SCRIPTS_DIR = templates.template(templates.SCRIPTS)  # wake.py, backup.py, narada-p.sh
MEMORY_TEMPLATE = templates.template(templates.MEMORY_TEMPLATE)  # identity tree skeleton

HOME = Path.home()
# Where installs made before the projects root was recorded linked projects
# from, unless --projects-root said otherwise. Never a default for a new
# install: see resolve_projects_root.
LEGACY_PROJECTS_ROOT = Path("C:/Projects")
INSTALL_RECORD = "install.json"


def default_memory_root() -> Path:
    """The memory root to install into when none is given: :func:`smriti.core.tree.tree_root`.

    ``$SMRITI_ROOT`` when set, else an existing ``~/.narada`` (an install
    made before 0.2), else ``~/.smriti``. Resolved when called, not at import.
    """
    from smriti.core.tree import tree_root

    return tree_root()


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
    if sys.platform == "win32":  # not is_windows(): type checkers narrow only sys.platform
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


# ── The projects root ──────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectsRoot:
    """The directory whose projects are linked into ``mirrors/``, and how it was chosen."""

    path: Path | None  # None: no projects root, so no mirrors are linked
    how: str  # "flag", "recorded", "mirrors", "legacy" or "none"
    note: str = ""  # what to tell the person, if anything

    @property
    def to_record(self) -> bool:
        """Whether this run should record it: chosen now, not read from the record.

        "none" is recorded too, so a rerun keeps it: without the record, the
        rerun would find the scripts this run deployed and take the install
        for one made before the record.
        """
        return self.how != "recorded"


def install_record_path(memory_root: Path) -> Path:
    return memory_root / ".smriti" / INSTALL_RECORD


def read_install_record(memory_root: Path) -> dict[str, Any]:
    """What earlier installs recorded; empty when nothing is, or it can't be read."""
    try:
        data = json.loads(install_record_path(memory_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_projects_root(memory_root: Path, projects_root: Path | None) -> Path | None:
    """Record *projects_root* for the next run, keeping the record's other keys.

    None records the choice of no projects root (``"projects_root": null``),
    which a rerun keeps; a record without the key is an install from before
    it. Returns the record's path, or None when it held this already.
    """
    from smriti import __version__

    data = read_install_record(memory_root)
    value = None if projects_root is None else str(projects_root)
    if "projects_root" in data and data["projects_root"] == value:
        return None
    data["projects_root"] = value
    data["recorded_by"] = __version__
    data["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = install_record_path(memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _link_target(link: Path) -> Path | None:
    try:
        target = os.readlink(link)
    except (OSError, ValueError):
        return None
    if target.startswith("\\\\?\\"):  # a junction's target, as Windows stores it
        target = target[4:]
    return Path(target)


def projects_root_from_mirrors(memory_root: Path) -> Path | None:
    """The projects root the existing mirrors link into, when they all agree.

    A mirror's ``ai`` link points at ``<projects root>/<name>/.ai`` and its
    ``knowledge`` link at ``<projects root>/<name>/.ai/knowledge``; links of
    any other shape are ignored. None when there are none, or they disagree.
    """
    try:
        mirrors = sorted((memory_root / "mirrors").iterdir())
    except OSError:
        return None
    found: dict[str, Path] = {}
    for mirror in mirrors:
        for leg, inner in (("ai", ""), ("knowledge", "knowledge")):
            target = _link_target(mirror / leg)
            if target is None:
                continue
            if inner:
                if target.name != inner:
                    continue
                target = target.parent
            if target.name == ".ai" and target.parent.name == mirror.name:
                root = target.parent.parent
                found.setdefault(os.path.normcase(str(root)), root)
    return next(iter(found.values())) if len(found) == 1 else None


def resolve_projects_root(memory_root: Path, given: str | None) -> ProjectsRoot:
    """The projects root this run links mirrors from.

    1. ``--projects-root`` when given (recorded for next time).
    2. The one ``install.json`` records, a directory or none (``null``).
    3. An install made before it was recorded (its record has no
       ``projects_root``): the root its mirrors link into, when they agree;
       else ``C:/Projects``, the old default, when that directory exists.
    4. Else none: a new install links no projects until it is told where
       they are.

    Whatever 1, 3 and 4 choose is recorded, so later runs link the same
    projects, or none, until a flag says otherwise.
    """
    if given:
        return ProjectsRoot(Path(given).expanduser().absolute(), "flag")
    record = read_install_record(memory_root)
    unset = ("no projects root, so no project mirrors are linked (recorded for the next "
             "run): pass --projects-root DIR to link them")
    if "projects_root" in record:
        recorded = record["projects_root"]
        if recorded is None:
            return ProjectsRoot(None, "recorded", unset)
        if isinstance(recorded, str) and recorded.strip():
            return ProjectsRoot(Path(recorded), "recorded")
    from smriti.install.privacy import is_existing_install

    if not is_existing_install(memory_root):
        return ProjectsRoot(None, "none", unset)
    inferred = projects_root_from_mirrors(memory_root)
    if inferred is not None:
        return ProjectsRoot(inferred, "mirrors",
                            f"projects root {inferred}, where the existing mirrors link; "
                            "recorded for the next run")
    if LEGACY_PROJECTS_ROOT.is_dir():
        return ProjectsRoot(LEGACY_PROJECTS_ROOT, "legacy",
                            f"projects root {LEGACY_PROJECTS_ROOT}, the default this install "
                            "was made with; recorded for the next run")
    return ProjectsRoot(None, "none", unset)


# ── Steps ────────────────────────────────────────────────────────────


def claude_project_dir(claude_dir: Path, project: Path) -> Path:
    """Claude Code's directory for the project at *project*.

    Claude Code names it after the project's path with every character
    outside ``[A-Za-z0-9]`` made ``-``: ``C:\\Projects\\my-app`` is
    ``C--Projects-my-app``, ``/home/sam/my.app`` is ``-home-sam-my-app``.
    """
    return claude_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))


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
        has_auto = (claude_project_dir(claude_dir, p) / "memory").is_dir()
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
    print(f"[mirrors] {len(projects)} project(s) with memory found under {projects_root}")
    for name in projects:
        proj_mirror = mirrors / name
        auto_target = claude_project_dir(claude_dir, projects_root / name) / "memory"
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
    template = MEMORY_TEMPLATE
    if template is None or not template.exists():
        print(f"[memory] template not found ({template or templates.MEMORY_TEMPLATE})")
        return
    copied = 0
    skipped = 0
    for src in template.rglob("*"):
        if src.is_dir() or "__pycache__" in src.parts:
            continue
        if src.name == ".gitkeep":
            rel = src.relative_to(template)
            (memory_root / rel.parent).mkdir(parents=True, exist_ok=True)
            continue
        if src.name == "README.md" and src.parent == template:
            continue
        rel = src.relative_to(template)
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
    names = ("wake.py", "backup.py", "narada-p.sh")
    scripts = SCRIPTS_DIR
    if scripts is None:
        for name in names:
            print(f"[wake] template missing: {templates.SCRIPTS}/{name}")
        return
    files = [(scripts / name, memory_root / ".smriti" / name) for name in names]
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


def run_core(memory_root: Path, projects_root: Path | None, *,
             skip_recall_daemon: bool = False) -> None:
    """Run all core install steps. Idempotent.

    With no *projects_root*, no project mirrors are linked.
    """
    if not memory_root.exists():
        print(f"[init] creating {memory_root}")
        memory_root.mkdir(parents=True, exist_ok=True)

    try:
        memory_root.relative_to(HOME)
    except ValueError:
        raise RuntimeError(
            f"--memory-root must be under {HOME} (got {memory_root})"
        ) from None

    install_memory_template(memory_root)
    install_wake_files(memory_root)
    if projects_root is not None:
        setup_mirrors(memory_root, projects_root)
    if not skip_recall_daemon:
        start_recall_daemon()

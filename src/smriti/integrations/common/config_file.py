"""Edits of a harness's own config file, without losing what anyone else writes.

``~/.claude.json``, ``~/.claude/settings.json`` and ``~/.codex/config.toml``
belong to the harness and the user: the harness rewrites them while it runs,
and they hold settings smriti must never lose. :func:`edit_config` changes
one so:

- **Locked.** It holds smriti's edit lock on the file from the read to the
  write, so two installs can't interleave. The harness takes no such lock,
  so:
- **Re-read before writing.** Just before it writes, it reads the file
  again. If it changed since the read, the edit starts over from the new
  content, once; if it changed again, nothing is written and the edit
  says why.
- **Backed up, the first backup kept.** Each change first copies the file
  as it was read to a new ``<name>.smriti-<UTC time>.bak`` beside it. The
  oldest of these, the file before smriti first changed it, is never
  removed; of the others, the newest are kept, :data:`BACKUPS` in all.
- **Atomic.** The new text replaces the file in one step
  (:func:`smriti.core.files.write_atomic`), with the file's permissions. A
  symbolic link (a dotfiles manager's) is kept: its target is what changes.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smriti.core.files import edit_lock, write_atomic

BACKUPS = 5  # the first backup and the newest four


@dataclass(frozen=True)
class Edited:
    """What :func:`edit_config` did."""

    changed: bool  # the file was rewritten
    backup: Path | None = None  # where the file as it was went
    refused: str = ""  # why the change wasn't written, when it wasn't


def edit_config(path: Path, change: Callable[[str | None], str | None]) -> Edited:
    """Rewrite *path* with ``change(its text)``, if that returns new text.

    *change* gets the file's text (None when there is no file) and returns
    the new text, or None to leave the file as it is. It may run twice: a
    second time on the file as the harness left it, when the harness wrote
    the file while the first ran.
    """
    target = path.resolve() if path.is_symlink() else path
    try:
        with edit_lock(target):
            for _attempt in range(2):
                before = _read(target)
                text = change(None if before is None else before.decode("utf-8"))
                if text is None:
                    return Edited(changed=False)
                if _read(target) != before:
                    continue  # the harness wrote it meanwhile: start again from its text
                backup = _backup(target, before) if before is not None else None
                write_atomic(target, text, mode=_mode(target))
                return Edited(changed=True, backup=backup)
    except OSError as exc:  # FileBusy among them: the lock or the file stayed busy
        return Edited(changed=False, refused=f"{target} was not changed: {exc}")
    return Edited(changed=False, refused=(
        f"{target} changed twice while smriti edited it, so it was left as it is; "
        "run the install again"))


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _mode(path: Path) -> int | None:
    """The file's permission bits, for its replacement; None on Windows or with no file."""
    if sys.platform == "win32":
        return None  # the directory's ACLs, inherited, decide there
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return None


def backups(path: Path) -> list[Path]:
    """The backups of *path* this module made, oldest first."""
    return sorted(path.parent.glob(f"{path.name}.smriti-*.bak"))


def _backup(path: Path, data: bytes) -> Path:
    """Write *data* (the file as read) to a new backup, never over one; then prune."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    mode = _mode(path)
    for n in range(1, 100):
        backup = path.with_name(f"{path.name}.smriti-{stamp}{'' if n == 1 else f'-{n}'}.bak")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                         0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                if mode is not None:
                    os.chmod(backup, mode)
                f.write(data)
        except BaseException:  # noqa: BLE001 — a part-written backup would pass for the first; re-raised
            backup.unlink(missing_ok=True)
            raise
        break
    else:
        raise FileExistsError(f"no free backup name for {path}")
    kept = backups(path)
    for old in kept[1:-(BACKUPS - 1)]:  # never the first; the newest BACKUPS - 1 after it
        old.unlink(missing_ok=True)
    return backup

"""The private per-user runtime directory, never inside the memory root.

- ``%LOCALAPPDATA%\\smriti\\run`` on Windows, private to the user by the
  profile's ACLs;
- ``$XDG_RUNTIME_DIR/smriti``, or else ``~/.cache/smriti/run``, elsewhere:
  created 0700, and refused when another user owns it or others can write
  to it.

``SMRITI_RUNTIME_DIR`` overrides it (tests use temporary directories).

It holds the embedding service's key and files (:mod:`smriti.embed.runtime`,
which re-exports these names), smriti's private locks
(:mod:`smriti.core.oslock`) and the profile lookup's cache
(:mod:`smriti.profiles`). This module needs nothing beyond the standard
library's basics, so a short-lived process (the wake hook) finds the
directory without importing the embedding protocol.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

RUNTIME_ENV = "SMRITI_RUNTIME_DIR"


class RuntimeDirError(PermissionError):
    """The runtime directory, or the key in it, isn't safe to use."""


def default_runtime_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "smriti" / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "smriti"
    return Path.home() / ".cache" / "smriti" / "run"


def private_dir_problem(st: os.stat_result, uid: int) -> str | None:
    """Why a directory with this ``lstat`` can't hold the key, or None when it can."""
    if stat.S_ISLNK(st.st_mode):
        return "it is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "it is not a directory"
    if st.st_uid != uid:
        return f"it is owned by uid {st.st_uid}, not by this user (uid {uid})"
    if st.st_mode & 0o022:
        return f"other users can write to it (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _verify_private_dir(path: Path) -> None:
    if sys.platform == "win32":
        return  # the profile's ACLs make %LOCALAPPDATA% the user's own (see SECURITY.md)
    st = os.lstat(path)
    problem = private_dir_problem(st, os.getuid())
    if problem:
        raise RuntimeDirError(f"refusing the runtime directory {path}: {problem}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)  # ours, and not writable by others: close it to them too


def runtime_dir(*, create: bool = True) -> Path:
    """The runtime directory, created and checked (only checked when ``create`` is False)."""
    env = os.environ.get(RUNTIME_ENV, "").strip()
    path = Path(env).expanduser() if env else default_runtime_dir()
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.lexists(path):
        _verify_private_dir(path)
    return path

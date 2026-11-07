"""An exclusive lock held by the operating system, not by a lock file's existence.

``msvcrt.locking`` on Windows and ``flock`` elsewhere: the kernel releases the
lock when its owner exits or crashes, so there is no stale lock to detect and
no takeover to race. Separate opens conflict even within one process, so
threads are serialised too. The lock file itself is never deleted.

Use it for short critical sections. A named lock (:func:`lock_path`) is one
file in the private per-user runtime directory (``smriti.core.rundir``, the one
the embedding service uses), in ``<runtime>/locks``: never in a directory
another user can write to, and never inside a git-tracked tree (a locked byte
range would make ``git add`` fail on Windows). Every process of the user
resolves the same runtime directory, whatever its environment, so they all
take the same file.

If the runtime directory is refused (another user owns it, or others can
write to it) or can't be created, a named lock isn't taken: :func:`try_lock`
yields :data:`UNAVAILABLE`, with a warning logged once. The other processes
take that file and no other, so a lock file anywhere else would exclude
nobody.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_LOCK_DIR = "locks"  # under the runtime directory
_warned: set[str] = set()


def private_lock_dir() -> Path:
    """``<runtime>/locks``, created and checked; raises OSError when it can't be used.

    The runtime directory's own checks (owner, mode, created 0700) come from
    ``smriti.core.rundir``, which refuses one that isn't safe with a
    ``RuntimeDirError`` (an OSError).
    """
    from smriti.core.rundir import RuntimeDirError, private_dir_problem, runtime_dir

    path = runtime_dir() / _LOCK_DIR
    path.mkdir(mode=0o700, exist_ok=True)
    if sys.platform != "win32":
        problem = private_dir_problem(os.lstat(path), os.getuid())
        if problem:
            raise RuntimeDirError(f"refusing the lock directory {path}: {problem}")
    return path


@dataclass(frozen=True)
class LockKey:
    """A named lock within a scope (e.g. a memory root, or a file's resolved path)."""

    name: str
    scope: str

    @property
    def filename(self) -> str:
        digest = hashlib.sha1(self.scope.encode("utf-8")).hexdigest()[:16]
        return f"{digest}-{self.name}.lock"

    def private(self) -> Path:
        """The lock's file in the private runtime directory (made and checked by this call)."""
        return private_lock_dir() / self.filename


def lock_path(name: str, scope: str) -> LockKey:
    """The per-user named lock ``name`` within ``scope``; hold it with :func:`exclusive_lock`."""
    return LockKey(name, scope)


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _warn_once(kind: str, message: str, *args: object) -> None:
    if kind not in _warned:
        _warned.add(kind)
        log.warning(message, *args)


def _open(path: Path, mode: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, mode)


def _open_named(key: LockKey) -> int | None:
    """Open a named lock's file; None when it can't be, so the lock can't be held."""
    try:
        return _open(key.private(), 0o600)
    except (OSError, RuntimeError) as exc:  # RuntimeError: Path.home() can't be found
        _warn_once(type(exc).__name__,
                   "the lock directory can't be used (%s), so named locks can't be taken", exc)
        return None


def _acquire(fd: int, deadline: float) -> bool:
    """Lock ``fd``, retrying until ``deadline`` (always one try)."""
    while True:
        try:
            _try_lock(fd)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _release(fd: int) -> None:
    try:
        _unlock(fd)
    except OSError:
        pass


#: What :func:`try_lock` yields.
HELD, BUSY, UNAVAILABLE = "held", "busy", "unavailable"


@contextmanager
def exclusive_lock(key: LockKey | Path, wait_s: float = 2.0) -> Iterator[bool]:
    """Hold an exclusive lock for the ``with`` block.

    ``key`` is a named lock from :func:`lock_path`, or a plain path (that file,
    e.g. the embedding service's lock in the runtime directory).

    Yields True when held. If it can't be taken within ``wait_s`` (or its file
    can't be opened), yields False and holds nothing; callers must honour that.
    """
    with try_lock(key, wait_s) as outcome:
        yield outcome == HELD


@contextmanager
def try_lock(key: LockKey | Path, wait_s: float = 2.0) -> Iterator[str]:
    """:func:`exclusive_lock`, saying why a lock isn't held.

    Yields :data:`HELD`; :data:`BUSY` when another holder kept it for all of
    ``wait_s``; or :data:`UNAVAILABLE` when its file couldn't be opened (for a
    named lock, a runtime directory that can't be used), so no holder can be
    excluded at all. Only HELD holds anything.
    """
    if isinstance(key, LockKey):
        fd = _open_named(key)
    else:
        try:
            fd = _open(key, 0o644)
        except OSError:
            fd = None
    if fd is None:
        yield UNAVAILABLE
        return
    try:
        held = _acquire(fd, time.monotonic() + wait_s)
        try:
            yield HELD if held else BUSY
        finally:
            if held:
                _release(fd)
    finally:
        os.close(fd)

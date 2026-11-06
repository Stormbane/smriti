"""An exclusive lock held by the operating system, not by a lock file's existence.

``msvcrt.locking`` on Windows and ``flock`` elsewhere: the kernel releases the
lock when its owner exits or crashes, so there is no stale lock to detect and
no takeover to race. Separate opens conflict even within one process, so
threads are serialised too. The lock file itself is never deleted.

Use it for short critical sections. A named lock (:func:`lock_path`) lives in
the private per-user runtime directory (``smriti.embed.runtime``, the one the
embedding service uses), in ``<runtime>/locks``: never in a directory another
user can write to, and never inside a git-tracked tree (a locked byte range
would make ``git add`` fail on Windows).

**The transition from the shared temp directory.** Releases before this one
kept named locks in ``<tempdir>/smriti-locks``, where another local user could
create or hold them, and long-lived processes (MCP servers, logd) keep running
that code after an upgrade. A new process taking only the new file would then
share a critical section (``core.files.edit_lock``, the day-log writer lock)
with an old one. So while :data:`LEGACY_LOCKS` is on, a named lock takes both
files, always the private one first and then the shared one:

- an old process holds only the shared file, so it and a new process exclude
  each other through that;
- two new processes exclude each other through the private file;
- the order never varies, so no process holds one file while waiting for the
  other in the opposite order; nested named locks keep whatever order their
  callers take them in.

**Dropping the shared file.** It is safe once no process running pre-transition
code can be alive: every MCP server, logd, scheduled task and hook has
restarted on new code (a reboot after upgrading is sure). A later release sets
:data:`LEGACY_LOCKS` to False. Before then, ``SMRITI_LEGACY_LOCKS=0`` drops it
for one install whose processes have all restarted (and ``=1`` keeps it after
the release). Every process of the user must also resolve the same runtime
directory: one started without ``XDG_RUNTIME_DIR`` (a cron job, say) uses
``~/.cache/smriti/run`` while a desktop session's processes use
``$XDG_RUNTIME_DIR/smriti``, and without the shared file they would not
exclude each other. Set ``SMRITI_RUNTIME_DIR`` for all of them before dropping it.

**When a file can't be used.** A named lock never takes a set of files its
rivals don't, which would let both into the critical section:

- If the runtime directory is refused (another user owns it, or others can
  write to it) or can't be created while the shared file is on, the lock
  falls back to the shared file alone, with a warning: every process takes
  that file, so it still excludes them all, as before the move.
- With the shared file dropped, other processes hold the private file
  alone, so failing to open it means the lock isn't taken (False), with a
  warning.
- If the shared file can't be opened during the transition, the lock isn't
  taken either (False), with a warning: an old process may hold it through a
  descriptor opened before its permissions changed, which a later failure
  to open doesn't revoke. On POSIX another user who owns the shared
  directory can cause this, as they could before the move; it ends when the
  shared file is dropped.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Whether named locks also take their pre-transition file in the shared temp
#: directory. See the module docstring before turning it off.
LEGACY_LOCKS = True
LEGACY_LOCKS_ENV = "SMRITI_LEGACY_LOCKS"

_LEGACY_DIR = "smriti-locks"  # under tempfile.gettempdir(); never change it while LEGACY_LOCKS
_PRIVATE_DIR = "locks"  # under the runtime directory
_warned: set[str] = set()


def legacy_locks() -> bool:
    """Whether named locks take the shared file too: ``SMRITI_LEGACY_LOCKS``, else the default."""
    value = os.environ.get(LEGACY_LOCKS_ENV, "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    return LEGACY_LOCKS


def private_lock_dir() -> Path:
    """``<runtime>/locks``, created and checked; raises OSError when it can't be used.

    The runtime directory's own checks (owner, mode, created 0700) come from
    ``smriti.embed.runtime``, which refuses one that isn't safe with a
    ``RuntimeDirError`` (an OSError).
    """
    from smriti.embed.runtime import RuntimeDirError, private_dir_problem, runtime_dir

    path = runtime_dir() / _PRIVATE_DIR
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

    @property
    def legacy(self) -> Path:
        """The file pre-transition code locks: exactly where it always has."""
        return Path(tempfile.gettempdir()) / _LEGACY_DIR / self.filename

    def private(self) -> Path:
        """The file in the private runtime directory (made and checked by this call)."""
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


def _open_named(key: LockKey) -> list[int]:
    """Open a named lock's files, in the order they are locked; empty when it can't be held.

    Never a namespace other processes don't take too: a process holding a
    file its rivals don't would share the critical section with them.
    """
    legacy = legacy_locks()
    fds: list[int] = []
    try:
        fds.append(_open(key.private(), 0o600))
    except (OSError, RuntimeError) as exc:  # RuntimeError: Path.home() can't be found
        if not legacy:
            # Without the shared file, others hold the private one alone: a
            # fallback to the shared file would be a lock nobody else takes.
            _warn_once(f"private-only:{type(exc).__name__}",
                       "the private lock directory can't be used (%s), and the shared lock "
                       "files are off (SMRITI_LEGACY_LOCKS), so named locks can't be taken", exc)
            return []
        _warn_once(f"private:{type(exc).__name__}",
                   "the private lock directory can't be used (%s); named locks fall back to "
                   "the shared temp directory alone", exc)
    if not legacy:
        return fds
    try:
        fds.append(_open(key.legacy, 0o644))
    except OSError as exc:
        # An old process may hold this file through a descriptor it opened
        # before the file or its directory changed: a later failure to open
        # it revokes neither, so the private file alone wouldn't exclude it.
        _warn_once(f"legacy:{type(exc).__name__}",
                   "the shared lock file can't be opened (%s); named locks can't be taken "
                   "while processes running older code may hold it", exc)
        for fd in fds:
            os.close(fd)
        return []
    return fds


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

    ``key`` is a named lock from :func:`lock_path` (its private file, and during
    the transition its shared one, in that order) or a plain path (that file
    alone, e.g. a lock that was always in the runtime directory).

    Yields True when held. If it can't be taken within ``wait_s`` (or no lock
    file can be opened), yields False and holds nothing; callers must honour
    that.
    """
    with try_lock(key, wait_s) as outcome:
        yield outcome == HELD


@contextmanager
def try_lock(key: LockKey | Path, wait_s: float = 2.0) -> Iterator[str]:
    """:func:`exclusive_lock`, saying why a lock isn't held.

    Yields :data:`HELD`; :data:`BUSY` when another holder kept it for all of
    ``wait_s``; or :data:`UNAVAILABLE` when no lock file could be opened (the
    module docstring's "When a file can't be used"), so no holder can be
    excluded at all. Only HELD holds anything.
    """
    if isinstance(key, LockKey):
        fds = _open_named(key)
    else:
        try:
            fds = [_open(key, 0o644)]
        except OSError:
            fds = []
    if not fds:
        yield UNAVAILABLE
        return
    held: list[int] = []
    try:
        deadline = time.monotonic() + wait_s
        for fd in fds:  # in order: never the shared file before the private one
            if not _acquire(fd, deadline):
                break
            held.append(fd)
        if len(held) < len(fds):
            for fd in reversed(held):
                _release(fd)
            held.clear()
        yield HELD if held else BUSY
    finally:
        for fd in reversed(held):
            _release(fd)
        for fd in fds:
            os.close(fd)

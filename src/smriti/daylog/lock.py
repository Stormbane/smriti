"""The day-log writer lock.

One OS-visible exclusive lock guards every write to the day-log or its
state file. Held per append burst, never for a process lifetime, so the
nightly task interleaves with a healthy running daemon (spec: review
round 2, finding 1).

Implementation: ``O_CREAT | O_EXCL`` lockfile containing the holder's
PID. Stale locks (holder no longer alive, or file older than
``STALE_AFTER_S``) are broken. Works on Windows and POSIX without
dependencies.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

STALE_AFTER_S = 300.0
_POLL_S = 0.05


class LockTimeout(RuntimeError):
    """The writer lock could not be acquired within the deadline."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-untyped]  # optional; never required

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        # tasklist is slow; os.kill(pid, 0) on Windows raises for dead
        # PIDs and PermissionError for live ones we can't signal.
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(lock_path: Path) -> int:
    try:
        return int(lock_path.read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        return 0


def _break_if_stale(lock_path: Path) -> None:
    pid = _read_pid(lock_path)
    stale_by_pid = pid != 0 and not _pid_alive(pid)
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return  # already gone
    if stale_by_pid or age > STALE_AFTER_S:
        with contextlib.suppress(OSError):
            lock_path.unlink()


@contextlib.contextmanager
def writer_lock(lock_path: Path, timeout_s: float = 10.0) -> Iterator[None]:
    """Acquire the exclusive writer lock, or raise :class:`LockTimeout`."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            _break_if_stale(lock_path)
            if time.monotonic() >= deadline:
                raise LockTimeout(f"writer lock busy: {lock_path}") from None
            time.sleep(_POLL_S)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()

"""An exclusive lock held by the operating system, not by a lock file's existence.

``msvcrt.locking`` on Windows and ``flock`` elsewhere: the kernel releases the
lock when its owner exits or crashes, so there is no stale lock to detect and
no takeover to race. Separate opens conflict even within one process, so
threads are serialised too. The lock file itself is never deleted.

Use it for short critical sections. Lock files live under the system temp
directory, keyed by name, so they never sit inside a git-tracked tree (a
locked byte range would make ``git add`` fail on Windows).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_path(name: str, scope: str) -> Path:
    """A per-machine lock file for ``name`` within ``scope`` (e.g. a memory root)."""
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "smriti-locks" / f"{digest}-{name}.lock"


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


@contextmanager
def exclusive_lock(path: Path, wait_s: float = 2.0) -> Iterator[bool]:
    """Hold an exclusive lock on ``path`` for the ``with`` block.

    Yields True when held. If it can't be taken within ``wait_s`` (or the
    lock file can't be opened), yields False and holds nothing; callers must
    honour that.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        yield False
        return
    held = False
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                _try_lock(fd)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield held
    finally:
        if held:
            try:
                _unlock(fd)
            except OSError:
                pass
        os.close(fd)

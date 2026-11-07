"""The day-log writer lock.

One exclusive lock guards every write to the day-log or its state file.
It is held per append burst, never for a process's lifetime, so the
nightly task interleaves with a healthy running daemon.

It is an OS-level lock (``smriti.core.oslock``): the kernel releases it
when its holder exits or crashes, so there is no stale lock to break and
no race over who takes it over, and a long holder is never broken by age.
The lock file lives in the private runtime directory (and, while the move
from the shared temp directory lasts, there too: see ``smriti.core.oslock``),
keyed by the configured lock path, not inside the memory tree, where a
locked byte range would make ``git add`` fail on Windows.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

class LockTimeout(RuntimeError):
    """The writer lock could not be acquired within the deadline."""


@contextlib.contextmanager
def writer_lock(lock_path: Path, timeout_s: float = 10.0) -> Iterator[None]:
    """Hold the exclusive writer lock for ``lock_path``, or raise :class:`LockTimeout`."""
    from smriti.core.oslock import exclusive_lock
    from smriti.core.oslock import lock_path as os_lock_path

    key = os_lock_path("daylog-writer", str(Path(lock_path).resolve()))
    with exclusive_lock(key, wait_s=timeout_s) as held:
        if not held:
            raise LockTimeout(f"writer lock busy: {lock_path}")
        yield

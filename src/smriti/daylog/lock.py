"""The day-log writer lock.

One exclusive lock guards every write to the day-log or its state file.
It is held per append burst, never for a process's lifetime, so the
nightly task interleaves with a healthy running daemon (spec: review
round 2, finding 1).

It is an OS-level lock (``smriti.core.oslock``): the kernel releases it
when its holder exits or crashes, so there is no stale lock to break and
no race over who takes it over, and a long holder is never broken by age.
The lock file lives in the temp directory, keyed by the configured lock
path, not inside the memory tree, where a locked byte range would make
``git add`` fail on Windows.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

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
    if sys.platform == "win32":  # sys.platform, so type checkers narrow it on every OS
        # Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, which
        # interrupts the target's console group instead of probing it.
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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

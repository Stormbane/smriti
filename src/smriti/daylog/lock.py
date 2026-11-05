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
import subprocess
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
    except ImportError:
        pass
    else:
        if not psutil.pid_exists(pid):
            return False
        try:
            return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
        except psutil.AccessDenied:
            return True  # it exists; we just may not look at it
        except psutil.Error:
            return False  # gone since pid_exists
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
    # An exited child its parent hasn't reaped yet still answers signal 0; it
    # isn't running, and a check that called it alive waited on it in vain.
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """Whether ``pid`` is an exited, unreaped process (POSIX; False if unknown)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        return stat.rsplit(")", 1)[-1].split()[0] == "Z"  # the state follows "(comm)"
    except (OSError, IndexError):
        pass
    try:  # no /proc (macOS, the BSDs)
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


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

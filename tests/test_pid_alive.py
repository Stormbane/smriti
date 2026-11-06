"""Process liveness: an exited child its parent hasn't reaped yet is not alive."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from smriti.core.procs import pid_alive


def test_a_running_process_is_alive_and_an_exited_one_is_not() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid)
    finally:
        proc.kill()
        proc.wait(10)
    assert not pid_alive(proc.pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX zombies")
def test_an_unreaped_child_is_not_alive() -> None:
    """CI on Linux: a stopped embedding service stayed a zombie until its parent
    reaped it, and `smriti embed stop` waited 60 s on it as if it were running."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and pid_alive(proc.pid):
            time.sleep(0.05)  # exits at once, but is not reaped: a zombie
        assert not pid_alive(proc.pid)
        assert proc.returncode is None  # still unreaped: it really was a zombie
    finally:
        proc.wait(10)


def test_nonpositive_pids_are_not_alive() -> None:
    assert not pid_alive(0) and not pid_alive(-1)

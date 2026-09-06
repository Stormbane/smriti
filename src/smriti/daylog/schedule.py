"""Scheduled-task management for the nightly cycle.

Ownership per the spec: Hermes owns scheduling once its cron home is
verified live; until then Windows Task Scheduler is the fallback. This
module manages the fallback tasks (and prints crontab lines on POSIX):

- ``smriti-logd``     every 10 min -> ``smriti logd --ensure`` (keepalive:
  starts the daemon if it is not running; worst-case 10-min capture gap
  after a crash, seconds-latency capture while alive)
- ``smriti-nightly``  daily 03:00  -> ``smriti nightly``
- ``smriti-morning``  daily 07:00  -> ``smriti morning``
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _windowless_python() -> str:
    """pythonw.exe beside the current interpreter, if it exists."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw) if pyw.exists() else sys.executable


def _smriti_cmd(args: str, *, windowless: bool = False) -> str:
    """Task command line. ``windowless`` uses pythonw so the tick doesn't
    flash a console window — only safe for tasks that spawn no console
    children of their own: a console child of a windowless parent
    allocates its OWN console, so nightly/morning (which run ``claude
    -p`` repeatedly) would flash MORE under pythonw, not less.
    """
    exe = _windowless_python() if windowless and os.name == "nt" else sys.executable
    return f'"{exe}" -m smriti {args}'


TASKS: list[tuple[str, str, list[str], bool]] = [
    # name, smriti args, schtasks schedule, windowless
    ("smriti-logd", "logd --ensure", ["/sc", "minute", "/mo", "10"], True),
    ("smriti-nightly", "nightly", ["/sc", "daily", "/st", "03:00"], False),
    ("smriti-morning", "morning", ["/sc", "daily", "/st", "07:00"], False),
]


def install_tasks() -> int:
    if os.name != "nt":
        print("POSIX: add these crontab lines (Hermes/cron owns scheduling):")
        print(f"  */10 * * * * {_smriti_cmd('logd --ensure')}")
        print(f"  0 3 * * *    {_smriti_cmd('nightly')}")
        print(f"  0 7 * * *    {_smriti_cmd('morning')}")
        return 0
    failures = 0
    for name, cmd, when, windowless in TASKS:
        result = subprocess.run(
            ["schtasks", "/create", "/f", "/tn", name,
             "/tr", _smriti_cmd(cmd, windowless=windowless), *when],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  {name}: scheduled ({' '.join(when)})")
        else:
            failures += 1
            print(f"  {name}: FAILED — {(result.stderr or result.stdout).strip()[:200]}")
    return 1 if failures else 0


def remove_tasks() -> int:
    if os.name != "nt":
        print("POSIX: remove the smriti crontab lines by hand.")
        return 0
    for name, _cmd, _when, _windowless in TASKS:
        subprocess.run(["schtasks", "/delete", "/f", "/tn", name], capture_output=True)
        print(f"  {name}: removed (if present)")
    return 0


def task_status() -> int:
    if os.name != "nt":
        print("POSIX: check crontab -l for the smriti lines.")
        return 0
    for name, _cmd, _when, _windowless in TASKS:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", name], capture_output=True, text=True
        )
        print(f"  {name}: {'scheduled' if result.returncode == 0 else 'NOT scheduled'}")
    return 0

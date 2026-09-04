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


def _smriti_cmd(args: str) -> str:
    return f'"{sys.executable}" -m smriti {args}'


TASKS: list[tuple[str, str, list[str]]] = [
    ("smriti-logd", "logd --ensure", ["/sc", "minute", "/mo", "10"]),
    ("smriti-nightly", "nightly", ["/sc", "daily", "/st", "03:00"]),
    ("smriti-morning", "morning", ["/sc", "daily", "/st", "07:00"]),
]


def install_tasks() -> int:
    if os.name != "nt":
        print("POSIX: add these crontab lines (Hermes/cron owns scheduling):")
        print(f"  */10 * * * * {_smriti_cmd('logd --ensure')}")
        print(f"  0 3 * * *    {_smriti_cmd('nightly')}")
        print(f"  0 7 * * *    {_smriti_cmd('morning')}")
        return 0
    failures = 0
    for name, cmd, when in TASKS:
        result = subprocess.run(
            ["schtasks", "/create", "/f", "/tn", name, "/tr", _smriti_cmd(cmd), *when],
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
    for name, _cmd, _when in TASKS:
        subprocess.run(["schtasks", "/delete", "/f", "/tn", name], capture_output=True)
        print(f"  {name}: removed (if present)")
    return 0


def task_status() -> int:
    if os.name != "nt":
        print("POSIX: check crontab -l for the smriti lines.")
        return 0
    for name, _cmd, _when in TASKS:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", name], capture_output=True, text=True
        )
        print(f"  {name}: {'scheduled' if result.returncode == 0 else 'NOT scheduled'}")
    return 0

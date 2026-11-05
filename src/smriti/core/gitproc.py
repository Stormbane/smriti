"""Is any git process running? Used before treating a git lock file as stale.

A git lock (``index.lock``, a ref lock) left by a killed git blocks every
later commit, but deleting one that a live git holds corrupts that git's
operation. So a lock is only ever cleared when no git process is running,
and a check that fails or can't tell counts as "running".
(``narada/.smriti/backup.py``, a standalone script, keeps its own copy.)
"""

from __future__ import annotations

import os
import subprocess


def git_running() -> bool:
    """True if any git process is running. A failed or unclear check counts as running."""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return proc.returncode != 0 or "git.exe" in proc.stdout.lower()
        # pgrep: 0 = found, 1 = none found, anything else = could not tell.
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True,
                              timeout=10, check=False).returncode != 1
    except (OSError, subprocess.SubprocessError):
        return True

"""Trigger git backup of the memory tree.

Wraps the `.smriti/backup.py` script deployed into the memory root. The
script is the one that actually runs `git commit` / `git push`. This
module exists so Python callers (writer, CLI) don't duplicate subprocess
boilerplate and all trigger points stay consistent.

Fire-and-forget: any failure is logged as a warning and goes no further.
Backup is best-effort; the next trigger catches up.
"""
from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

if sys.platform == "win32":
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _CREATE_NO_WINDOW = 0


def trigger(
    tag: str,
    *,
    push: bool = False,
    root: Path | None = None,
    timeout: int = 900,
) -> None:
    """Invoke the memory-tree backup script.

    Parameters
    ----------
    tag:
        Short label for the commit message (e.g. "write", "nightly").
        Commit messages are never encrypted (even with git-crypt, which
        the installer doesn't set up), so keep tags generic.
    push:
        If True, also push to origin after committing.
    root:
        Memory tree root. If omitted, resolves via tree_root(). Callers
        operating on non-default roots (tests, custom entities) must pass
        root explicitly so the backup runs in the right repo.
    """
    try:
        if root is None:
            from smriti.core.tree import tree_root
            root = tree_root()
        else:
            root = Path(root)

        script = root / ".smriti" / "backup.py"
        if not script.exists():
            return
        # --foreground: callers (the nightly witness) need the commit to exist
        # when this returns. Hooks call the script directly and get the
        # detached default instead.
        args = [sys.executable, str(script), "--tag", tag, "--foreground"]
        if push:
            args.append("--push")
        env = os.environ.copy()
        env["SMRITI_ROOT"] = str(root)
        # Wait, but never kill: killing the script mid-run cuts off its
        # commit/push/logging, and a killed git leaves .git/*.lock behind.
        # On timeout the script is left to finish on its own.
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — best-effort; the next trigger catches up
        log.warning("backup (%s) did not run: %s", tag, exc)

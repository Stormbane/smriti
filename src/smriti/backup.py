"""Trigger git backup of the memory tree.

Wraps the `.smriti/backup.py` script deployed into the memory root. The
script is the one that actually runs `git commit` / `git push`. This
module exists so Python callers (writer, CLI) don't duplicate subprocess
boilerplate and all trigger points stay consistent.

Fire-and-forget: any failure is swallowed. Backup is best-effort; the
next trigger catches up.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def trigger(
    tag: str,
    *,
    push: bool = False,
    root: Path | None = None,
    timeout: int = 120,
) -> None:
    """Invoke the memory-tree backup script.

    Parameters
    ----------
    tag:
        Short label for the commit message (e.g. "write", "sleep-start").
        Content is encrypted; tags are not. Keep them generic.
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
        args = [sys.executable, str(script), "--tag", tag]
        if push:
            args.append("--push")
        env = os.environ.copy()
        env["SMRITI_ROOT"] = str(root)
        subprocess.run(
            args,
            timeout=timeout,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except Exception:
        pass

"""Idempotent hook script deployment.

Both Claude Code (``~/.claude/hooks/``) and Codex (``~/.codex/hooks/``
or ``<repo>/.codex/hooks/``) run hooks as subprocesses pointed at
scripts on disk. The scripts are tiny shims that delegate to the
relevant ``smriti.<area>.hook`` entry point — same pattern, same
deployment ergonomics.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def deploy_hook_scripts(
    src_dir: Path,
    dst_dir: Path,
    names: list[str],
) -> list[Path]:
    """Copy ``names`` from ``src_dir`` to ``dst_dir`` if changed.

    Creates ``dst_dir`` if missing. Skips files whose bytes already
    match the source (so re-running install is a no-op except for
    drift). Returns the destination paths actually written.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in names:
        src = src_dir / name
        dst = dst_dir / name
        if not src.exists():
            print(f"[hooks] source missing: {src}")
            continue
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            print(f"[hooks] {name} up to date")
            continue
        shutil.copy2(src, dst)
        print(f"[hooks] deployed {name} -> {dst}")
        written.append(dst)
    return written


__all__ = ["deploy_hook_scripts"]

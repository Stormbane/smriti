"""Idempotent hook script deployment + drift detection.

Both Claude Code (``~/.claude/hooks/``) and Codex (``~/.codex/hooks/``
or ``<repo>/.codex/hooks/``) run hooks as subprocesses pointed at
scripts on disk. The scripts are tiny shims that delegate to the
relevant ``smriti.<area>.hook`` entry point — same pattern, same
deployment ergonomics.

Deployment is deliberately copy-based, not symlink-based: true file
symlinks on Windows require Developer Mode / elevation (WinError 1314
otherwise), git-bash ``ln -s`` silently degrades to a copy, and the
editable pip install already single-sources all real logic — the shims
are stable entry points whose text rarely changes. Byte-compare on
deploy plus :func:`check_deployed_hooks` in doctor covers the drift
that copies can accumulate.
"""

from __future__ import annotations

import os
from pathlib import Path


def deploy_hook_scripts(
    src_dir: Path,
    dst_dir: Path,
    names: list[str] | dict[str, str],
) -> list[Path]:
    """Copy hook scripts from ``src_dir`` to ``dst_dir`` if changed.

    ``names`` is either a list (same filename both sides) or a mapping
    of ``src_name -> dst_name`` so one shared shim can deploy under a
    harness-local filename. Creates ``dst_dir`` if missing, skips files
    whose bytes already match, and writes via temp file + ``os.replace``
    so an interrupted deploy never leaves a truncated hook. Returns the
    destination paths actually written.
    """
    mapping = names if isinstance(names, dict) else {n: n for n in names}
    dst_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for src_name, dst_name in mapping.items():
        src = src_dir / src_name
        dst = dst_dir / dst_name
        if not src.exists():
            print(f"[hooks] source missing: {src}")
            continue
        payload = src.read_bytes()
        if dst.exists() and dst.read_bytes() == payload:
            print(f"[hooks] {dst_name} up to date")
            continue
        tmp = dst.with_name(dst.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, dst)
        print(f"[hooks] deployed {src_name} -> {dst}")
        written.append(dst)
    return written


def check_deployed_hooks(
    src_dir: Path,
    dst_dir: Path,
    names: list[str] | dict[str, str],
) -> list[str]:
    """Read-only integrity check: deployed bytes must match sources.

    Returns a list of problems (empty = healthy). Used by doctor for
    BOTH harnesses so a stale, truncated, or hand-edited deployed shim
    is visible — the shims swallow their own failures by design, so
    drift is otherwise user-invisible.
    """
    mapping = names if isinstance(names, dict) else {n: n for n in names}
    problems: list[str] = []
    for src_name, dst_name in mapping.items():
        src = src_dir / src_name
        dst = dst_dir / dst_name
        if not src.exists():
            problems.append(f"source missing: {src}")
            continue
        if not dst.exists():
            problems.append(f"not deployed: {dst}")
            continue
        try:
            if src.read_bytes() != dst.read_bytes():
                problems.append(f"drifted from source: {dst}")
        except OSError as exc:
            problems.append(f"unreadable: {dst} ({exc})")
    return problems


__all__ = ["deploy_hook_scripts", "check_deployed_hooks"]

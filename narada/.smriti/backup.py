#!/usr/bin/env python
"""Backup helper for the narada memory tree.

Quietly commits — and optionally pushes — the SMRITI_ROOT git repo.
Called from:
  - SessionEnd hook      -> --push
  - smriti sleep start   -> --push
  - smriti wake          -> --push
  - smriti_write path    -> (commit only)

Content is git-crypt encrypted; commit messages are NOT. Keep tags generic
(session-end / sleep / wake / write) so metadata leaks nothing.

Best-effort: push failures (no network, conflict) exit 0 and log to stderr.
Next trigger catches up.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(os.environ.get("SMRITI_ROOT", Path.home() / ".narada"))


def run_git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "git not found"


def has_changes(cwd: Path) -> bool:
    code, out = run_git(["status", "--porcelain"], cwd)
    return code == 0 and bool(out.strip())


def do_commit(cwd: Path, tag: str) -> bool:
    if not has_changes(cwd):
        return False
    run_git(["add", "-A"], cwd)
    code, _ = run_git(["commit", "-q", "-m", f"snapshot: {tag}"], cwd)
    return code == 0


def do_push(cwd: Path) -> tuple[bool, str]:
    code, out = run_git(["push", "-q", "origin", "master"], cwd, timeout=90)
    return code == 0, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="manual", help="commit message tag")
    parser.add_argument("--push", action="store_true", help="push after commit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cwd = repo_root()
    if not (cwd / ".git").exists():
        if args.verbose:
            print(f"[backup] no git repo at {cwd}", file=sys.stderr)
        return 0

    committed = do_commit(cwd, args.tag)
    if args.verbose:
        msg = "created" if committed else "nothing to commit"
        print(f"[backup] commit: {msg}", file=sys.stderr)

    if args.push:
        ok, out = do_push(cwd)
        if args.verbose:
            if ok:
                print("[backup] push: ok", file=sys.stderr)
            else:
                print(f"[backup] push failed (will retry next trigger): {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

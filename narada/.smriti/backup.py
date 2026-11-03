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

Best-effort: failures exit 0 and the next trigger catches up. Every outcome
is appended to ``.git/smriti-backup.jsonl`` (inside .git, so the log never
creates a commit of its own) for `smriti audit` and humans to read.

Git is never killed. A git process killed mid-operation leaves
``.git/index.lock`` or a ref lock behind, and every later git call then fails
quietly: that stopped or misreported backups three times in 2026. On a
timeout, git is left to finish on its own; locks older than LOCK_STALE_S are
cleared only when no git process is running.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK_STALE_S = 15 * 60
LOG_NAME = "smriti-backup.jsonl"
LOG_MAX_BYTES = 200_000
LOG_KEEP_LINES = 1000


def repo_root() -> Path:
    return Path(os.environ.get("SMRITI_ROOT", Path.home() / ".narada"))


def log_event(cwd: Path, **fields: object) -> None:
    """Append one JSON line to .git/smriti-backup.jsonl. Never raises."""
    path = cwd / ".git" / LOG_NAME
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if path.stat().st_size > LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-LOG_KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def run_cmd(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run *cmd* without ever killing it.

    Output goes to a temp file rather than a pipe, so a process left running
    after a timeout can't die on a broken pipe when this script exits.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            return 127, f"{cmd[0]} not found"
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"{' '.join(cmd[:2])} still running after {timeout:g}s; left to finish"
        out.seek(0)
        return proc.returncode, out.read().strip()


def run_git(args: list[str], cwd: Path, timeout: float = 120) -> tuple[int, str]:
    return run_cmd(["git", *args], cwd, timeout)


def git_running() -> bool:
    """True if any git process is running. Unknown counts as running."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return "git.exe" in out.lower()
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def stale_locks(cwd: Path, now: float | None = None) -> list[Path]:
    git = cwd / ".git"
    now = time.time() if now is None else now
    candidates = [git / "index.lock", git / "HEAD.lock", git / "packed-refs.lock",
                  git / "config.lock", git / "objects" / "maintenance.lock"]
    if (git / "refs").is_dir():
        candidates += sorted((git / "refs").rglob("*.lock"))
    return [p for p in candidates if p.is_file() and now - p.stat().st_mtime > LOCK_STALE_S]


def clear_stale_locks(cwd: Path) -> None:
    stale = stale_locks(cwd)
    if not stale:
        return
    names = [p.relative_to(cwd).as_posix() for p in stale]
    if git_running():
        log_event(cwd, event="stale_lock_kept", locks=names, reason="git process running")
        return
    removed = []
    for p in stale:
        try:
            p.unlink()
            removed.append(p.relative_to(cwd).as_posix())
        except OSError:
            pass
    log_event(cwd, event="stale_lock_cleared", locks=removed)


def has_changes(cwd: Path) -> bool:
    code, out = run_git(["status", "--porcelain"], cwd)
    return code == 0 and bool(out.strip())


def do_commit(cwd: Path, tag: str) -> bool:
    if not has_changes(cwd):
        return False
    code, out = run_git(["add", "-A"], cwd, timeout=300)
    if code != 0:
        log_event(cwd, event="add_failed", tag=tag, code=code, out=out[-500:])
        return False
    code, out = run_git(["commit", "-q", "-m", f"snapshot: {tag}"], cwd, timeout=300)
    log_event(cwd, event="commit", tag=tag, ok=code == 0, code=code, out=out[-500:])
    return code == 0


def do_push(cwd: Path) -> tuple[bool, str]:
    code, out = run_git(["push", "-q", "origin", "master"], cwd, timeout=300)
    log_event(cwd, event="push", ok=code == 0, code=code, out=out[-500:])
    return code == 0, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="manual", help="commit message tag")
    parser.add_argument("--push", action="store_true", help="push after commit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Skip when invoked from a smriti-internal `claude -p` subprocess.
    # SessionEnd hooks fire on every subprocess exit; pushing on each one
    # piles up and gets cancelled, breaking smriti's own pipelines.
    if os.environ.get("SMRITI_INTERNAL"):
        return 0

    cwd = repo_root()
    if not (cwd / ".git").exists():
        if args.verbose:
            print(f"[backup] no git repo at {cwd}", file=sys.stderr)
        return 0

    clear_stale_locks(cwd)
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

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
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOCK_STALE_S = 15 * 60
LOG_NAME = "smriti-backup.jsonl"
LOG_MAX_BYTES = 200_000
LOG_KEEP_LINES = 1000
# One backup at a time. The run lock is broken only when its owner is dead,
# or after RUN_LOCK_MAX_AGE_S: far beyond any real run, and only there to
# survive PID reuse after a crash.
RUN_LOCK_NAME = "smriti-backup.lock"
RUN_LOCK_MAX_AGE_S = 6 * 3600
RUN_LOCK_INIT_GRACE_S = 60


def repo_root() -> Path:
    return Path(os.environ.get("SMRITI_ROOT", Path.home() / ".narada"))


_GIT_DIRS: dict[str, tuple[Path, Path]] = {}


def git_dirs(cwd: Path) -> tuple[Path, Path]:
    """(git dir, common dir). In a linked worktree ``.git`` is a file, not a directory."""
    key = str(cwd)
    if key not in _GIT_DIRS:
        dot = cwd / ".git"
        dirs = (dot, dot)
        if not dot.is_dir():
            code, out = run_git(["rev-parse", "--absolute-git-dir", "--git-common-dir"], cwd,
                                timeout=30)
            lines = out.splitlines()
            if code == 0 and len(lines) >= 2:
                common = Path(lines[1])
                dirs = (Path(lines[0]), (common if common.is_absolute() else cwd / common).resolve())
        _GIT_DIRS[key] = dirs
    return _GIT_DIRS[key]


def log_event(cwd: Path, **fields: object) -> None:
    """Append one JSON line to <git common dir>/smriti-backup.jsonl. Never raises."""
    path = git_dirs(cwd)[1] / LOG_NAME
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
    """True if any git process is running. A failed or unclear check counts as running."""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode != 0 or "git.exe" in proc.stdout.lower()
        # pgrep: 0 = found, 1 = none found, anything else = could not tell.
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True,
                              timeout=10).returncode != 1
    except (OSError, subprocess.SubprocessError):
        return True


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # optional

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        # Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT.
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_run_lock(cwd: Path) -> str | None:
    """Take the one-backup-at-a-time lock. Returns its token, or None if busy.

    The token is written to a temp file first and then hard-linked into
    place, so the lock never exists without its owner's token. Filesystems
    without hard links fall back to O_EXCL, where a fresh empty lock (an
    owner mid-write) counts as busy.
    """
    path = git_dirs(cwd)[1] / RUN_LOCK_NAME
    token = f"{os.getpid()} {uuid.uuid4().hex}"
    tmp = path.with_name(f"{RUN_LOCK_NAME}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(token, encoding="ascii")
    except OSError:
        return None
    try:
        for _ in range(2):
            try:
                os.link(tmp, path)
                return token
            except FileExistsError:
                if not _break_dead_run_lock(path):
                    return None
            except OSError:
                return _acquire_excl(path, token)
        return None
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _acquire_excl(path: Path, token: str) -> str | None:
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _break_dead_run_lock(path):
                return None
            continue
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token)
        return token
    return None


def _break_dead_run_lock(path: Path) -> bool:
    try:
        held = path.read_text(encoding="ascii")
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True  # vanished in between: just retry
    first = held.split()[0] if held.split() else ""
    if not first.isdigit():
        if age < RUN_LOCK_INIT_GRACE_S:
            return False  # empty or partial: its owner may still be writing the token
        owner_alive = False
    else:
        owner_alive = pid_alive(int(first))
    if owner_alive and age < RUN_LOCK_MAX_AGE_S:
        return False
    try:
        if path.read_text(encoding="ascii") == held:  # still the lock we judged dead
            path.unlink()
    except OSError:
        pass
    return True


def release_run_lock(cwd: Path, token: str) -> None:
    path = git_dirs(cwd)[1] / RUN_LOCK_NAME
    try:
        if path.read_text(encoding="ascii") == token:  # only ever our own
            path.unlink()
    except OSError:
        pass


def _identity(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_ino, st.st_mtime_ns, st.st_size


def stale_locks(cwd: Path, now: float | None = None) -> list[tuple[Path, tuple[int, int, int]]]:
    """Git lock files older than LOCK_STALE_S, each with the identity it had when seen."""
    git, common = git_dirs(cwd)
    now = time.time() if now is None else now
    candidates = [git / "index.lock", git / "HEAD.lock", common / "packed-refs.lock",
                  common / "config.lock", common / "objects" / "maintenance.lock"]
    if (common / "refs").is_dir():
        candidates += sorted((common / "refs").rglob("*.lock"))
    found = []
    for p in candidates:
        try:
            if p.is_file() and now - p.stat().st_mtime > LOCK_STALE_S:
                found.append((p, _identity(p)))
        except OSError:
            pass
    return found


def clear_stale_locks(cwd: Path) -> None:
    """Remove stale git locks. Callers hold the backup run lock."""
    stale = stale_locks(cwd)
    if not stale:
        return
    names = [p.relative_to(cwd).as_posix() for p, _ in stale]
    if git_running():
        log_event(cwd, event="stale_lock_kept", locks=names, reason="git process running")
        return
    removed = []
    for p, seen in stale:
        try:
            if _identity(p) == seen:  # a fresh lock in its place belongs to a live git
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


def spawn_detached(argv: list[str]) -> bool:
    """Re-run this script as a detached worker; True if it started.

    Hooks call backup.py and a harness may kill the hook when its timeout
    expires, taking git down with it: the run lock and index.lock it held
    are then left behind. A detached worker isn't in the hook's process
    tree (or, on Windows, its job), so the kill never reaches it.
    """
    cmd = [sys.executable, os.path.abspath(__file__), *argv, "--worker"]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        # Breakaway is what escapes a kill-on-close job; DETACHED_PROCESS and a
        # new process group don't change job membership. If the job forbids
        # breakaway, report failure so the caller runs in the foreground.
        # (Outside any job the breakaway flag is simply ignored.)
        detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
        try:
            subprocess.Popen(cmd, creationflags=detached | new_group | breakaway, **kwargs)
            return True
        except OSError:
            return False
    try:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="manual", help="commit message tag")
    parser.add_argument("--push", action="store_true", help="push after commit")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--foreground", action="store_true",
                        help="do the work in this process and wait (callers that need the "
                             "commit to exist when this returns, e.g. the nightly witness)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
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

    # Detach by default; --verbose stays in the foreground so its output is seen.
    if not (args.foreground or args.worker or args.verbose):
        forwarded = ["--tag", args.tag] + (["--push"] if args.push else [])
        if spawn_detached(forwarded):
            return 0
        log_event(cwd, event="detach_failed", tag=args.tag)  # fall through: run here

    token = acquire_run_lock(cwd)
    if token is None:
        log_event(cwd, event="skipped", tag=args.tag, reason="another backup is running")
        return 0
    try:
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
                    print(f"[backup] push failed (will retry next trigger): {out}",
                          file=sys.stderr)
    finally:
        release_run_lock(cwd, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())

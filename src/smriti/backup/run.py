"""One backup run: commit the memory tree, and push it when asked and there is something new.

``python -m smriti.backup [--root DIR] [--tag TAG] [--push]``. The code runs
from the installed package, never from the memory tree, so a restore of the
tree or a write to it can't change what a backup runs. ``smriti backup``
(the hourly ``smriti-backup`` task) and the nightly's witness step start it
through :func:`smriti.backup.trigger`, in a process of its own, so a caller
can stop waiting for it without killing it, and read what it did from the log.

A push sends the current branch to its upstream (``origin`` and the same
branch name when it has none yet; :func:`smriti.core.gitproc.push_target`);
a detached HEAD isn't pushed. A run with nothing new costs one ``git status``,
logs that it found nothing, and makes no network call.

If the repo uses git-crypt (recommended; the installer doesn't set it up),
content is encrypted but commit messages are NOT. Keep tags generic
(hourly / nightly / witness / manual) so metadata leaks nothing.

Best-effort: a run exits 0 whatever git did, and the next one catches up.
Every outcome is appended to ``<git common dir>/smriti-backup.jsonl`` (inside
.git, so the log never creates a commit of its own), "nothing to commit" and
"up to date" included: a run that logged neither a success nor a failure is
not taken as a success. A caller that sets SMRITI_BACKUP_RUN gets that id on
each line of its run, so it can tell what its own run did.

Git is never killed (:func:`smriti.core.gitproc.run_git`): on a timeout it
is left to finish on its own, and locks older than LOCK_STALE_S are cleared
only when no git process is running.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smriti.backup import LOG_NAME, git_dirs
from smriti.core.gitproc import PushTarget, git_running, push_target, run_git
from smriti.core.procs import pid_alive

LOCK_STALE_S = 15 * 60
LOG_MAX_BYTES = 200_000
LOG_KEEP_LINES = 1000
# One backup at a time: an OS lock on RUN_LOCK_NAME in the git common dir,
# held for the run (see acquire_run_lock). The file is never deleted.
RUN_LOCK_NAME = "smriti-backup.lock"
# For the token an older backup.py, whose lock was this file's existence, left
# in it: a run of one this old is a crashed one whose pid was reused (no run
# lasts near that long).
RUN_LOCK_MAX_AGE_S = 6 * 3600
# The third field of this run's token, so a later run tells it from an older script's.
RUN_LOCK_KIND = "os-lock"
RUN_LOCK_BUSY = "another backup is running"


def log_event(cwd: Path, **fields: object) -> None:
    """Append one JSON line to <git common dir>/smriti-backup.jsonl. Never raises."""
    dirs = git_dirs(cwd)
    if dirs is None:
        return
    path = dirs[1] / LOG_NAME
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    run = os.environ.get("SMRITI_BACKUP_RUN", "")
    if run:
        record["run"] = run[:64]
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if path.stat().st_size > LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-LOG_KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        """Lock byte 0 of *fd*'s file, or raise OSError at once."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _held_elsewhere(exc: OSError) -> bool:
        """Whether a failed :func:`_lock` means another holder (not a lock that can't work)."""
        return exc.errno in (errno.EACCES, errno.EDEADLOCK)

else:
    import fcntl

    def _lock(fd: int) -> None:
        """Lock *fd*'s file, or raise OSError at once."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _held_elsewhere(exc: OSError) -> bool:
        """Whether a failed :func:`_lock` means another holder (not a lock that can't work)."""
        return isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES)


@dataclass(frozen=True)
class RunLock:
    """The run lock, held until :func:`release_run_lock` or the process ends."""

    fd: int
    path: Path


def acquire_run_lock(cwd: Path) -> RunLock | str:
    """Take the one-backup-at-a-time lock for this run, or say why not.

    An OS lock (``msvcrt.locking`` on Windows, ``flock`` elsewhere) on
    RUN_LOCK_NAME in the git common dir, so every worktree shares it, as
    :mod:`smriti.core.oslock`'s locks are held. The kernel drops it when its
    holder exits or crashes: a crashed run leaves nothing to break, and a live
    one is never taken over, however long it runs. Nothing deletes the file,
    since a contender that did could leave two holders of two files. Never
    waits: a busy lock is a skipped run.

    An older backup.py made the lock the file's existence, holding "<pid> <hex>",
    broken by a contender that judged its owner dead and deleted it: between
    that check and the delete a third run could take the lock, and the delete
    removed the new owner's. That token of a live owner (a run of the older
    script still going when this one starts) is honoured, unless it is
    RUN_LOCK_MAX_AGE_S old. This run's token, "<pid> <hex> os-lock", in turn
    reads to an older script as a live owner's while this run holds the lock.
    A file that such a script deleted between this run's open and its lock
    is let go, and the one at the path locked instead.
    """
    dirs = git_dirs(cwd)
    if dirs is None:
        return f"no git directory at {cwd}"
    path = dirs[1] / RUN_LOCK_NAME
    for _ in range(3):
        lock = _take(path)
        if isinstance(lock, str) or _is_the_file(lock.fd, path):
            break
        release_run_lock(lock)  # deleted or replaced since the open
    else:
        return RUN_LOCK_BUSY
    if isinstance(lock, str):
        return lock
    fd = lock.fd
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        held = os.read(fd, 256).decode("ascii", errors="replace")
        if _older_run_alive(held, os.fstat(fd).st_mtime):
            release_run_lock(lock)
            return RUN_LOCK_BUSY
        token = f"{os.getpid()} {uuid.uuid4().hex} {RUN_LOCK_KIND}\n".encode("ascii")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, token)
    except OSError:
        pass  # the OS lock is held; the token only informs an older script
    return lock


def _take(path: Path) -> RunLock | str:
    """Open *path* and lock it, or say why not."""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        return f"the run lock can't be opened: {exc.strerror or exc}"
    try:
        _lock(fd)
    except OSError as exc:
        os.close(fd)
        if _held_elsewhere(exc):
            return RUN_LOCK_BUSY
        return f"the run lock can't be taken: {exc.strerror or exc}"
    return RunLock(fd, path)


def _is_the_file(fd: int, path: Path) -> bool:
    """Whether *path* still names the file open as *fd*."""
    try:
        held, named = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


def _older_run_alive(held: str, mtime: float) -> bool:
    """Whether *held* is the token of an older backup.py, and that run still goes.

    That script wrote "<pid> <hex>" and deleted the file when done, so a
    token it left is a crashed run's unless its pid is alive, and a pid
    alive after RUN_LOCK_MAX_AGE_S has been reused.
    """
    fields = held.split()
    if len(fields) != 2 or not fields[0].isdigit():
        return False
    return time.time() - mtime < RUN_LOCK_MAX_AGE_S and pid_alive(int(fields[0]))


def release_run_lock(lock: RunLock) -> None:
    """Release the run lock. The file stays: the next run takes the same one."""
    try:
        _unlock(lock.fd)
    except OSError:
        pass
    finally:
        os.close(lock.fd)


def _identity(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return st.st_ino, st.st_mtime_ns, st.st_size


def _shown(path: Path, cwd: Path) -> str:
    """*path* as the log names it: relative to the tree when it is inside it."""
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:  # a linked worktree's locks are in the main repository's .git
        return path.as_posix()


def stale_locks(cwd: Path, now: float | None = None) -> list[tuple[Path, tuple[int, int, int]]]:
    """Git lock files older than LOCK_STALE_S, each with the identity it had when seen."""
    dirs = git_dirs(cwd)
    if dirs is None:
        return []
    git, common = dirs
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
    names = [_shown(p, cwd) for p, _ in stale]
    if git_running():
        log_event(cwd, event="stale_lock_kept", locks=names, reason="git process running")
        return
    removed = []
    for p, seen in stale:
        try:
            if _identity(p) == seen:  # a fresh lock in its place belongs to a live git
                p.unlink()
                removed.append(_shown(p, cwd))
        except OSError:
            pass
    log_event(cwd, event="stale_lock_cleared", locks=removed)


def has_changes(cwd: Path, tag: str = "") -> bool | None:
    """Whether there is anything to commit; None when `git status` failed, which is logged.

    A failed status is not "nothing to commit": an unreadable index would
    otherwise stop every backup while each run reported nothing to do.
    """
    code, out = run_git(["status", "--porcelain"], cwd)
    if code != 0:
        log_event(cwd, event="status_failed", tag=tag, code=code, out=out[-500:])
        return None
    return bool(out.strip())


def do_commit(cwd: Path, tag: str) -> str:
    """Commit what changed: "ok", "nothing to commit" or "failed", each one logged."""
    changed = has_changes(cwd, tag)
    if changed is None:
        return "failed"
    if not changed:
        log_event(cwd, event="nothing_to_commit", tag=tag)
        return "nothing to commit"
    code, out = run_git(["add", "-A"], cwd, timeout=300)
    if code != 0:
        log_event(cwd, event="add_failed", tag=tag, code=code, out=out[-500:])
        return "failed"
    code, out = run_git(["commit", "-q", "-m", f"snapshot: {tag}"], cwd, timeout=300)
    log_event(cwd, event="commit", tag=tag, ok=code == 0, code=code, out=out[-500:])
    return "ok" if code == 0 else "failed"


def do_push(cwd: Path, target: PushTarget, tag: str = "") -> tuple[bool, str]:
    code, out = run_git(target.args, cwd, timeout=300)
    log_event(cwd, event="push", tag=tag, ok=code == 0, code=code, out=out[-500:],
              to=f"{target.remote} {target.refspec}")
    return code == 0, out


def needs_push(cwd: Path, tracking: str) -> bool:
    """False only when HEAD is known to be on ``tracking`` (the push target) already.

    Any doubt (no remote-tracking ref yet, a git error) means push.
    """
    code, out = run_git(["rev-list", "--count", f"{tracking}..HEAD"], cwd, timeout=30)
    return code != 0 or out.strip() != "0"


def _push(cwd: Path, tag: str) -> None:
    """Push when there is something new, logging what was done or why not."""
    try:
        target = push_target(cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        target = f"could not read the current branch: {str(exc)[-200:]}"
    if isinstance(target, str):
        log_event(cwd, event="push_skipped", tag=tag, reason=target)
    elif not needs_push(cwd, target.tracking):
        log_event(cwd, event="up_to_date", tag=tag)
    else:
        do_push(cwd, target, tag)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m smriti.backup",
        description="One backup run of the memory tree; `smriti backup` runs it and "
                    "reports what it did.")
    parser.add_argument("--root", help="the memory tree (default: smriti's memory root)")
    parser.add_argument("--tag", default="manual", help="commit message tag")
    parser.add_argument("--push", action="store_true", help="push after commit")
    args = parser.parse_args(argv)
    if args.root:
        cwd = Path(args.root)
    else:
        from smriti.core.tree import tree_root

        cwd = tree_root()
    if not (cwd / ".git").exists():
        return 0

    lock = acquire_run_lock(cwd)
    if isinstance(lock, str):
        log_event(cwd, event="skipped", tag=args.tag, reason=lock)
        return 0
    try:
        clear_stale_locks(cwd)
        do_commit(cwd, args.tag)
        if args.push:
            _push(cwd, args.tag)
    finally:
        release_run_lock(lock)
    return 0

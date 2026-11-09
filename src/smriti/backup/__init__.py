"""Back the memory tree up with git, and read how backups went.

:func:`trigger` starts one backup run, ``python -m smriti.backup``
(:mod:`smriti.backup.run`, which runs ``git commit`` and ``git push``), in a
process of its own, and waits for it: ``smriti backup`` (the hourly task)
and the nightly's witness step call it. The run's code is the installed
package's; nothing in the memory tree is run.

Best effort: a backup that can't run is logged as a warning and goes no
further, and the next trigger catches up. The run appends every outcome to
``<git common dir>/smriti-backup.jsonl``. :func:`trigger` reads back what
its own run did (the nightly's witness step records it), and
:func:`backup_status` the last commit and push, whoever ran them (the
hourly task, the nightly), for ``smriti status`` and the nightly's status,
so a failed push shows where the wake and the CLI look, not only in that log.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)

# subprocess is imported by the functions that run git or the backup:
# `smriti status` reads the backup log through this module and doesn't
# import it (tests/test_import_costs.py).

LOG_NAME = "smriti-backup.jsonl"  # in the git common dir
_RUN_ENV = "SMRITI_BACKUP_RUN"  # the run puts it on each line it logs
# What trigger runs, after the interpreter: -P keeps the current directory
# (which may be the memory tree) off the import path.
_WORKER: tuple[str, ...] = ("-P", "-m", "smriti.backup")
_DETAIL_CHARS = 300
_NO_OUTCOME = "the run logged no outcome"


@dataclass(frozen=True)
class BackupRun:
    """What one :func:`trigger` did, from the lines its run logged."""

    ran: bool  # the run went to its end
    # "ok", "failed", "nothing to commit", "skipped", "unknown" (the run logged no outcome)
    commit: str = ""
    push: str = ""  # "ok", "failed", "up to date", "skipped", "not asked", "unknown"
    detail: str = ""  # why it didn't run, or git's output for a failure

    @property
    def ok(self) -> bool:
        """It ran, and its lines show each step done: a step it didn't log isn't ok."""
        return self.ran and not {self.commit, self.push} & {"failed", "unknown"}

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": self.ok, "commit": self.commit, "push": self.push}
        if self.detail:
            out["detail"] = self.detail
        if not self.ran:
            out["ran"] = False
        return out


def trigger(
    tag: str,
    *,
    push: bool = False,
    root: Path | None = None,
    timeout: int = 900,
) -> BackupRun:
    """Run one backup of the memory tree (``python -m smriti.backup``); what it did.

    Parameters
    ----------
    tag:
        Short label for the commit message (e.g. "write", "nightly").
        Commit messages are never encrypted (even with git-crypt, which
        the installer doesn't set up), so keep tags generic.
    push:
        If True, also push after committing: the current branch to its
        upstream, or to ``origin`` under the same name when it has none.
    root:
        Memory tree root. If omitted, resolves via tree_root(). Callers
        operating on non-default roots (tests, custom entities) must pass
        root explicitly so the backup runs in the right repo.
    timeout:
        How long to wait for the run. One still going then is left to
        finish, never killed, and reported as not done.
    """
    try:
        if root is None:
            from smriti.core.tree import tree_root
            root = tree_root()
        else:
            root = Path(root)

        # Reasons to do nothing, which the run wouldn't log.
        if os.environ.get("SMRITI_INTERNAL"):
            return BackupRun(ran=False, detail="not run inside smriti's own LLM calls")
        if not (root / ".git").exists():
            return BackupRun(ran=False, detail=f"no git repository at {root}")
        return _run(root, tag, push=push, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — best-effort; the next trigger catches up
        log.warning("backup (%s) did not run: %s", tag, exc)
        return BackupRun(ran=False, detail=f"did not run: {exc}"[:_DETAIL_CHARS])


def _run(root: Path, tag: str, *, push: bool, timeout: int) -> BackupRun:
    import subprocess
    import uuid

    args = [sys.executable, *_WORKER, "--root", str(root), "--tag", tag]
    if push:
        args.append("--push")
    run_id = uuid.uuid4().hex[:12]
    env = os.environ.copy()
    env["SMRITI_ROOT"] = str(root)
    env[_RUN_ENV] = run_id
    # Wait, but never kill: killing the run mid-way cuts off its
    # commit/push/logging, and a killed git leaves .git/*.lock behind.
    # On timeout the run is left to finish on its own.
    flags = 0
    if sys.platform == "win32":  # a statement, which mypy narrows on every platform
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=flags,
    )
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Not a success: the commit and push may still fail. Never killed (above).
        return BackupRun(ran=False, detail=f"still running after {timeout}s; left to finish")
    events = _events_of_run(backup_log_path(root), run_id)
    if code != 0:
        # A run exits 0 whatever git did, so this is the run itself failing
        # (it couldn't start, or raised): what it logged first, if anything,
        # and nothing it didn't log taken as done.
        done = _summarise(events, push=push, finished=False)
        why = f"the backup exited {code}" + (f"; {done.detail}" if done.detail else "")
        return BackupRun(ran=False, commit=done.commit, push=done.push,
                         detail=why[:_DETAIL_CHARS])
    return _summarise(events, push=push)


def _events_of_run(log_path: Path | None, run_id: str) -> list[dict[str, object]]:
    """The lines a run logged: those that carry its id."""
    return [e for e in _read_events(log_path) if e.get("run") == run_id]


def _summarise(events: list[dict[str, object]], *, push: bool,
               finished: bool = True) -> BackupRun:
    """What a run's lines say it did; a step it didn't log is not taken as done.

    "nothing to commit" and "up to date" are lines of their own, so a failed
    ``git status`` doesn't read as a success, nor does a run whose lines are
    lost: a finished run with no line for a step reports it "unknown".
    """
    commit, push_state = "", "not asked" if finished and not push else ""
    detail = ""
    for e in events:
        kind = e.get("event")
        if kind == "skipped":
            return BackupRun(ran=True, commit="skipped", push="skipped",
                             detail=str(e.get("reason", "")))
        if kind == "commit":
            commit = "ok" if e.get("ok") else "failed"
        elif kind in ("add_failed", "status_failed"):
            commit = "failed"
        elif kind == "nothing_to_commit":
            commit = "nothing to commit"
        elif kind == "push":
            push_state = "ok" if e.get("ok") else "failed"
        elif kind == "up_to_date":
            push_state = "up to date"
        elif kind == "push_skipped":
            push_state, detail = "skipped", str(e.get("reason", ""))
        if kind in ("commit", "add_failed", "status_failed", "push") and not e.get("ok"):
            detail = _detail(e)
    if finished and "" in (commit, push_state):
        commit, push_state = commit or "unknown", push_state or "unknown"
        detail = detail or _NO_OUTCOME
    return BackupRun(ran=True, commit=commit, push=push_state, detail=detail)


def _detail(event: dict[str, object]) -> str:
    """A failure's reason in one line: git's last lines of output, or the exit code."""
    out = " ".join(str(event.get("out") or "").split())
    if not out:
        out = f"git exited {event.get('code', '?')}"
    if event.get("event") == "status_failed":
        out = f"git status failed: {out}"
    return out[-_DETAIL_CHARS:]


# ── what the log says ─────────────────────────────────────────────────


def git_dirs(root: Path) -> tuple[Path, Path] | None:
    """The repository's git directory and its common one; None without a repository.

    ``.git`` is a directory, or in a linked worktree a file naming the
    worktree's git directory, whose ``commondir`` names the common one, where
    the backup's log and run lock are. Read from the files, as git would,
    without running git.
    """
    dot = root / ".git"
    try:
        if dot.is_dir():
            return dot, dot
        text = dot.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    gitdir = gitdir if gitdir.is_absolute() else root / gitdir
    try:
        common = Path((gitdir / "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return gitdir, gitdir
    return gitdir, (common if common.is_absolute() else (gitdir / common).resolve())


def git_common_dir(root: Path) -> Path | None:
    """The repository's common git directory, where the backup log is; None without one."""
    dirs = git_dirs(root)
    return dirs[1] if dirs is not None else None


def backup_log_path(root: Path) -> Path | None:
    common = git_common_dir(root)
    return common / LOG_NAME if common is not None else None


def _read_lines(path: Path | None, offset: int = 0) -> list[str]:
    """The log's lines from byte *offset* on, oldest first."""
    if path is None:
        return []
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _records(lines: Iterable[str]) -> Iterator[dict[str, object]]:
    """Each line's record, in the lines' order; lines that aren't records skipped."""
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _read_events(path: Path | None, offset: int = 0) -> list[dict[str, object]]:
    """The log's records from byte *offset* on, oldest first."""
    return list(_records(_read_lines(path, offset)))


@dataclass(frozen=True)
class Outcome:
    """The last attempt at a commit or a push."""

    state: str  # "ok", "failed", "skipped" (a push with nowhere to go)
    at: str  # when, as the log records it (ISO 8601, UTC)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state != "failed"


@dataclass(frozen=True)
class BackupStatus:
    """The last backup commit and push the log records."""

    repo: bool  # the root is a git repository
    commit: Outcome | None = None
    push: Outcome | None = None
    failing_since: str = ""  # when the pushes began failing ("" unless the last failed)

    @property
    def ok(self) -> bool:
        """False when the last commit or the last push failed."""
        return all(o is None or o.ok for o in (self.commit, self.push))

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": self.ok}
        if not self.repo:
            out["repo"] = False
        for name, outcome in (("commit", self.commit), ("push", self.push)):
            if outcome is not None:
                out[name] = {"state": outcome.state, "at": outcome.at,
                             **({"detail": outcome.detail} if outcome.detail else {})}
        if self.failing_since:
            out["failing_since"] = self.failing_since
        return out

    def describe(self, tz: tzinfo | None = None) -> str:
        """One line for ``smriti status``."""
        if not self.repo:
            return "off (the memory root is not a git repository)"
        if self.commit is None and self.push is None:
            return "no backup recorded yet"
        parts = []
        if self.commit is not None:
            parts.append(_line("commit", self.commit, tz))
        if self.push is None:
            parts.append("no push recorded")
        elif self.push.state == "failed":
            since = self.failing_since or self.push.at
            line = f"push FAILED since {_when(since, tz)}"
            if since != self.push.at:
                line += f" (last tried {_when(self.push.at, tz)})"
            parts.append(line + (f": {self.push.detail}" if self.push.detail else ""))
        else:
            parts.append(_line("push", self.push, tz))
        return "; ".join(parts)


def _when(at: str, tz: tzinfo | None) -> str:
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return at
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _line(name: str, outcome: Outcome, tz: tzinfo | None) -> str:
    state = "FAILED" if outcome.state == "failed" else outcome.state
    line = f"{name} {state} {_when(outcome.at, tz)}"
    return line + (f": {outcome.detail}" if outcome.detail else "")


def _push_target(event: dict[str, object]) -> str:
    """The remote (a name, or a URL or path) a push line went to: its ``to`` less the refspec."""
    to = str(event.get("to", "")).strip()
    remote, _, _refspec = to.rpartition(" ")  # a refspec holds no space; a path may
    return remote or to


def _remote_configured(root: Path, target: str) -> bool:
    """Whether a push to *target* (a remote's name, or a URL or path) has somewhere to go.

    Git answers, from its effective configuration (included files, the global
    and worktree configs), by the installer's rule for the backup's remote
    (:func:`smriti.install.privacy.push_has_destination`). When it can't say,
    the push counts as having somewhere to go, so its failure stays a failure.
    """
    import subprocess

    from smriti.install.privacy import push_has_destination

    if not target:
        return True
    try:
        return push_has_destination(root, target)
    except (OSError, subprocess.SubprocessError):
        return True


def backup_status(root: Path) -> BackupStatus:
    """The last commit and push in the backup log, and since when pushes fail.

    A push that failed because its remote isn't configured at all (no
    upstream and no ``origin``: the run then tries ``origin``) is
    reported as skipped, not failed: such an install has nowhere to push,
    which isn't a failure of its backups. Git decides whether the remote is
    configured (:func:`_remote_configured`); it is asked only when the last
    push failed. Pushes that were skipped (a detached HEAD) are reported so too.

    A failed ``git status`` is a failed commit. A later run that found
    nothing to commit ends the failures before it: the commit reported is
    then the last one made.
    """
    common = git_common_dir(root)
    if common is None:
        return BackupStatus(repo=False)
    commit: Outcome | None = None
    push: Outcome | None = None
    failing_since = ""
    streak_found = False  # the push before the failures, or the log's start
    commit_cleared = False  # a later run found nothing to commit
    # Newest first, parsing only as far back as the answer needs.
    for e in _records(reversed(_read_lines(common / LOG_NAME))):
        kind, at = e.get("event"), str(e.get("at", ""))
        if commit is None and kind == "nothing_to_commit":
            commit_cleared = True
        elif commit is None and kind in ("commit", "add_failed", "status_failed"):
            ok = kind == "commit" and bool(e.get("ok"))
            if ok or not commit_cleared:
                commit = Outcome("ok" if ok else "failed", at, "" if ok else _detail(e))
        elif push is None and kind == "push_skipped":
            push = Outcome("skipped", at, str(e.get("reason", "")))
        elif push is None and kind == "push":
            if e.get("ok"):
                push = Outcome("ok", at)
            else:
                target = _push_target(e)
                if _remote_configured(root, target):
                    push, failing_since = Outcome("failed", at, _detail(e)), at
                else:
                    push = Outcome("skipped", at, f"no remote {target!r} is configured")
        elif (push is not None and push.state == "failed" and kind == "push"
              and not streak_found):
            # Back through the failures to the push before them; past that one,
            # older failures are another streak, however long the scan goes on
            # for the last commit.
            if e.get("ok"):
                streak_found = True
            else:
                failing_since = at
        if (commit is not None and push is not None
                and (push.state != "failed" or streak_found)):
            break
    return BackupStatus(repo=True, commit=commit, push=push, failing_since=failing_since)

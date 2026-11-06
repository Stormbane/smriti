"""Trigger git backup of the memory tree, and read how backups went.

Wraps the `.smriti/backup.py` script deployed into the memory root. The
script is the one that actually runs `git commit` / `git push`. This
module exists so Python callers (writer, CLI) don't duplicate subprocess
boilerplate and all trigger points stay consistent.

Fire-and-forget: a backup that can't run is logged as a warning and goes
no further, and the next trigger catches up. The script appends every
outcome to ``<git dir>/smriti-backup.jsonl``. :func:`trigger` reads back
what its own run did (the nightly's witness step records it), and
:func:`backup_status` the last commit and push, whoever ran them (the
hourly task, the nightly), for ``smriti status`` and the nightly's status
(hardening H12.13: before, a failed push was only in that log).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path

log = logging.getLogger(__name__)

LOG_NAME = "smriti-backup.jsonl"  # the script's LOG_NAME
_RUN_ENV = "SMRITI_BACKUP_RUN"  # the script puts it on each line of the run
_DETAIL_CHARS = 300


@dataclass(frozen=True)
class BackupRun:
    """What one :func:`trigger` did, from the lines its run logged."""

    ran: bool  # the script ran to its end
    commit: str = ""  # "ok", "failed", "nothing to commit", "skipped"
    push: str = ""  # "ok", "failed", "up to date", "skipped", "not asked"
    detail: str = ""  # why it didn't run, or git's output for a failure

    @property
    def ok(self) -> bool:
        return self.ran and "failed" not in (self.commit, self.push)

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
    """Invoke the memory-tree backup script; returns what the run did.

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
    """
    try:
        if root is None:
            from smriti.core.tree import tree_root
            root = tree_root()
        else:
            root = Path(root)

        script = root / ".smriti" / "backup.py"
        if not script.exists():
            return BackupRun(ran=False, detail=f"no backup script at {script}")
        # The script's own reasons to do nothing, which it doesn't log.
        if os.environ.get("SMRITI_INTERNAL"):
            return BackupRun(ran=False, detail="not run inside smriti's own LLM calls")
        if not (root / ".git").exists():
            return BackupRun(ran=False, detail=f"no git repository at {root}")
        return _run_script(script, root, tag, push=push, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — best-effort; the next trigger catches up
        log.warning("backup (%s) did not run: %s", tag, exc)
        return BackupRun(ran=False, detail=f"did not run: {exc}"[:_DETAIL_CHARS])


def _run_script(script: Path, root: Path, tag: str, *, push: bool, timeout: int) -> BackupRun:
    import subprocess
    import uuid

    # --foreground: callers (the nightly witness) need the commit to exist
    # when this returns. Hooks call the script directly and get the
    # detached default instead.
    args = [sys.executable, str(script), "--tag", tag, "--foreground"]
    if push:
        args.append("--push")
    run_id = uuid.uuid4().hex[:12]
    env = os.environ.copy()
    env["SMRITI_ROOT"] = str(root)
    env[_RUN_ENV] = run_id
    log_path = backup_log_path(root)
    start = _size(log_path)
    started = _now_iso()
    # Wait, but never kill: killing the script mid-run cuts off its
    # commit/push/logging, and a killed git leaves .git/*.lock behind.
    # On timeout the script is left to finish on its own.
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return BackupRun(ran=False, detail=f"still running after {timeout}s; left to finish")
    events = _events_of_run(log_path, run_id, start, started, tag)
    return _summarise(events, push=push)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def _events_of_run(log_path: Path | None, run_id: str, start: int, started: str,
                   tag: str) -> list[dict[str, object]]:
    """The lines this run logged.

    By the run id when the script writes one. An older script's lines carry
    none: then the lines added since the run began (all of the log's lines
    from its start time on, if the log was trimmed meanwhile), less the
    tagged lines of other runs. Backups run one at a time, so those are this
    run's, unless another run that was finishing as this one began logged
    its push after that.
    """
    trimmed = _size(log_path) < start  # the script keeps the log's last lines only
    events = _read_events(log_path, offset=0 if trimmed else start)
    mine = [e for e in events if e.get("run") == run_id]
    if mine:
        return mine
    if trimmed:
        events = [e for e in events if str(e.get("at", "")) >= started]
    return [e for e in events if not e.get("run") and e.get("tag", tag) in (tag, "")]


def _summarise(events: list[dict[str, object]], *, push: bool) -> BackupRun:
    commit, push_state, detail = "nothing to commit", "up to date" if push else "not asked", ""
    for e in events:
        kind = e.get("event")
        if kind == "skipped":
            return BackupRun(ran=True, commit="skipped", push="skipped",
                             detail=str(e.get("reason", "")))
        if kind == "commit":
            commit = "ok" if e.get("ok") else "failed"
        elif kind == "add_failed":
            commit = "failed"
        elif kind == "push":
            push_state = "ok" if e.get("ok") else "failed"
        elif kind == "push_skipped":
            push_state, detail = "skipped", str(e.get("reason", ""))
        if kind in ("commit", "add_failed", "push") and not e.get("ok"):
            detail = _detail(e)
    return BackupRun(ran=True, commit=commit, push=push_state, detail=detail)


def _detail(event: dict[str, object]) -> str:
    """A failure's reason in one line: git's last lines of output, or the exit code."""
    out = " ".join(str(event.get("out") or "").split())
    if not out:
        out = f"git exited {event.get('code', '?')}"
    return out[-_DETAIL_CHARS:]


# ── what the log says ─────────────────────────────────────────────────


def git_common_dir(root: Path) -> Path | None:
    """The repository's common git directory, where the backup log is; None without one.

    ``.git`` is a directory, or in a linked worktree a file naming the
    worktree's git directory, whose ``commondir`` names the common one.
    Read from the files, as git would, without running git.
    """
    dot = root / ".git"
    try:
        if dot.is_dir():
            return dot
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
        return gitdir
    return common if common.is_absolute() else (gitdir / common).resolve()


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


# Compiled on first use (re caches it): only a failed push needs it.
_REMOTE_PATTERN = r'^\s*\[\s*remote\s+"([^"]+)"\s*\]'


def _remote_configured(common: Path, target: str) -> bool:
    """Whether a push to *target* (a remote's name, or a URL or path) has somewhere to go."""
    if not target or any(c in target for c in "/\\:"):
        return True  # a URL or a path: it names its own destination
    try:
        config = (common / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return target in re.findall(_REMOTE_PATTERN, config, re.MULTILINE | re.IGNORECASE)


def backup_status(root: Path) -> BackupStatus:
    """The last commit and push in the backup log, and since when pushes fail.

    A push that failed because its remote isn't configured at all (no
    upstream and no ``origin``: the script then tries ``origin``) is
    reported as skipped, not failed: such an install has nowhere to push,
    which isn't a failure of its backups. Pushes that were skipped (a
    detached HEAD) are reported so too.
    """
    common = git_common_dir(root)
    if common is None:
        return BackupStatus(repo=False)
    commit: Outcome | None = None
    push: Outcome | None = None
    failing_since = ""
    streak_found = False  # the push before the failures, or the log's start
    # Newest first, parsing only as far back as the answer needs.
    for e in _records(reversed(_read_lines(common / LOG_NAME))):
        kind, at = e.get("event"), str(e.get("at", ""))
        if commit is None and kind in ("commit", "add_failed"):
            ok = kind == "commit" and bool(e.get("ok"))
            commit = Outcome("ok" if ok else "failed", at, "" if ok else _detail(e))
        elif push is None and kind == "push_skipped":
            push = Outcome("skipped", at, str(e.get("reason", "")))
        elif push is None and kind == "push":
            if e.get("ok"):
                push = Outcome("ok", at)
            else:
                target = str(e.get("to", "")).split(" ")[0]
                if _remote_configured(common, target):
                    push, failing_since = Outcome("failed", at, _detail(e)), at
                else:
                    push = Outcome("skipped", at, f"no remote {target!r} is configured")
        elif push is not None and push.state == "failed" and kind == "push":
            if e.get("ok"):
                streak_found = True
            else:
                failing_since = at
        if (commit is not None and push is not None
                and (push.state != "failed" or streak_found)):
            break
    return BackupStatus(repo=True, commit=commit, push=push, failing_since=failing_since)

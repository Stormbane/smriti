"""Running git for the backup and the installer: never killing it, and where a push goes.

A git process killed mid-operation leaves ``.git/index.lock`` or a ref lock
behind, and every later git call then fails quietly, so :func:`run_git`
waits for git and, on a timeout, leaves it to finish rather than kill it.
For the same reason a lock is only ever cleared when no git process is
running (:func:`git_running`), and a check that fails or can't tell counts
as "running". :func:`read_git` is for commands that only read and take no
lock, where a timeout may end them.

:func:`push_target` is the one rule for where the backup pushes the current
branch; the installer's privacy check asks about the same remote through it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# CREATE_NO_WINDOW: a hidden console the child inherits. Without it, a
# console-less parent's git calls (the windowless hourly task's) each get a
# fresh visible console: a blank window that steals focus for half a second.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def git_running() -> bool:
    """True if any git process is running. A failed or unclear check counts as running."""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, check=False,
                creationflags=NO_WINDOW,
            )
            return proc.returncode != 0 or "git.exe" in proc.stdout.lower()
        # pgrep: 0 = found, 1 = none found, anything else = could not tell.
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True,
                              timeout=10, check=False).returncode != 1
    except (OSError, subprocess.SubprocessError):
        return True


def run_cmd(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run *cmd* without ever killing it: its exit code and its output, stderr included.

    Output goes to a temp file rather than a pipe, so a process left running
    after a timeout can't die on a broken pipe when its caller exits. A
    timeout returns 124 and says the process was left to finish; a command
    that can't be found returns 127.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=NO_WINDOW)
        except FileNotFoundError:
            return 127, f"{cmd[0]} not found"
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"{' '.join(cmd[:2])} still running after {timeout:g}s; left to finish"
        out.seek(0)
        return proc.returncode, out.read().strip()


def run_git(args: list[str], cwd: Path, timeout: float = 120) -> tuple[int, str]:
    """``git <args>`` in *cwd* by :func:`run_cmd`: never killed."""
    return run_cmd(["git", *args], cwd, timeout)


def read_git(cwd: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """``git <args>`` in *cwd* for a command that only reads: stdout and stderr apart.

    Only for commands that take no lock (``config --get``, ``symbolic-ref``):
    a timeout ends them, which leaves nothing behind.
    """
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=timeout, check=False, creationflags=NO_WINDOW,
    )


@dataclass(frozen=True)
class PushTarget:
    """Where the backup pushes the current branch."""

    remote: str  # a remote's name, or the URL or path an upstream names
    refspec: str  # "<branch's ref>:<the ref it updates there>"
    tracking: str  # what holds the remote's copy here: "@{u}", or refs/remotes/origin/<branch>
    set_upstream: bool  # no upstream yet: the push makes this one the branch's upstream

    @property
    def args(self) -> list[str]:
        """The ``git push`` arguments."""
        return ["push", "-q", *(["-u"] if self.set_upstream else []), self.remote, self.refspec]


def push_target(cwd: Path) -> PushTarget | str:
    """Where the current branch goes, or why nothing is pushed.

    The branch's upstream when it has one (``branch.<b>.remote`` and
    ``branch.<b>.merge``). Without one, ``origin`` and a branch of the same
    name, and the push makes that the upstream. Never a fixed branch name: a
    repository on ``main`` pushed to ``master`` failed on every run. A
    detached HEAD, or one on a ref that isn't a branch, pushes nothing, and
    the reason is returned instead. Raises SubprocessError when git can't say
    what HEAD is.
    """
    head = read_git(cwd, "symbolic-ref", "-q", "HEAD")
    ref = head.stdout.strip()
    if head.returncode == 1:
        return "HEAD is detached"
    if head.returncode != 0:
        raise subprocess.SubprocessError(
            head.stderr.strip()[-300:] or f"git symbolic-ref exited {head.returncode}")
    if not ref.startswith("refs/heads/"):
        return f"HEAD is on {ref}, not a branch"
    branch = ref[len("refs/heads/"):]
    remote = read_git(cwd, "config", "--get", f"branch.{branch}.remote")
    merge = read_git(cwd, "config", "--get", f"branch.{branch}.merge")
    if (remote.returncode == 0 and merge.returncode == 0
            and remote.stdout.strip() and merge.stdout.strip()):
        return PushTarget(remote.stdout.strip(), f"{ref}:{merge.stdout.strip()}", "@{u}",
                          set_upstream=False)
    return PushTarget("origin", f"{ref}:{ref}", f"refs/remotes/origin/{branch}",
                      set_upstream=True)

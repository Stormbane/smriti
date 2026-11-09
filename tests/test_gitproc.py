"""Tests for running git: a check that can't tell must say "running", git is never
killed, and the push target is the current branch's upstream, else origin."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import smriti.core.gitproc as gitproc


def _ran(returncode: int, stdout: str = "") -> Any:
    return lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize("returncode,stdout,expect", [
    (0, '"git.exe","1234","Console","1","50,000 K"\n', True),
    (0, "INFO: No tasks are running which match the specified criteria.\n", False),
    (1, "", True),
])
def test_windows_trusts_only_a_successful_listing(
        monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, expect: bool) -> None:
    monkeypatch.setattr(gitproc.os, "name", "nt")
    monkeypatch.setattr(gitproc.subprocess, "run", _ran(returncode, stdout))
    assert gitproc.git_running() is expect


@pytest.mark.parametrize("returncode,expect", [(0, True), (1, False), (2, True)])
def test_posix_pgrep_exit_codes(monkeypatch: pytest.MonkeyPatch,
                                returncode: int, expect: bool) -> None:
    monkeypatch.setattr(gitproc.os, "name", "posix")
    monkeypatch.setattr(gitproc.subprocess, "run", _ran(returncode))
    assert gitproc.git_running() is expect


def test_a_check_that_cannot_run_counts_as_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("tasklist")

    monkeypatch.setattr(gitproc.subprocess, "run", missing)
    assert gitproc.git_running() is True


def test_timeout_leaves_the_process_running(tmp_path: Path) -> None:
    done = tmp_path / "done"
    code, msg = gitproc.run_cmd(
        [sys.executable, "-c",
         f"import time; time.sleep(2); open({str(done)!r}, 'w').write('x')"],
        tmp_path, timeout=0.3,
    )
    assert code == 124 and "left to finish" in msg
    deadline = time.time() + 15
    while not done.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert done.exists(), "the timed-out process was killed"


def test_run_cmd_returns_output_and_code(tmp_path: Path) -> None:
    code, out = gitproc.run_cmd(
        [sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], tmp_path, timeout=30,
    )
    assert code == 3 and out == "hi"
    assert gitproc.run_cmd(["no-such-command-anywhere"], tmp_path, timeout=30)[0] == 127


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "tree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo


@needs_git
def test_a_branch_with_an_upstream_is_pushed_there(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "config", "branch.main.remote", "mirror")
    _git(repo, "config", "branch.main.merge", "refs/heads/trunk")
    target = gitproc.push_target(repo)
    assert target == gitproc.PushTarget("mirror", "refs/heads/main:refs/heads/trunk", "@{u}",
                                        set_upstream=False)
    assert isinstance(target, gitproc.PushTarget)
    assert target.args == ["push", "-q", "mirror", "refs/heads/main:refs/heads/trunk"]


@needs_git
def test_a_branch_without_one_goes_to_origin_under_its_own_name(tmp_path: Path) -> None:
    target = gitproc.push_target(_repo(tmp_path))
    assert isinstance(target, gitproc.PushTarget)
    assert target.args == ["push", "-q", "-u", "origin", "refs/heads/main:refs/heads/main"]
    assert target.tracking == "refs/remotes/origin/main"


@needs_git
def test_a_detached_head_pushes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "-c", "user.name=Ari", "-c", "user.email=ari@example.com",
         "commit", "-q", "--allow-empty", "-m", "first")
    _git(repo, "checkout", "-q", "--detach")
    assert gitproc.push_target(repo) == "HEAD is detached"


@needs_git
def test_a_push_target_git_cannot_read_raises(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(subprocess.SubprocessError):
        gitproc.push_target(tmp_path)  # not a repository

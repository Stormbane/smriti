"""Tests for git_running: a check that can't tell must say "running"."""

from __future__ import annotations

import subprocess
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

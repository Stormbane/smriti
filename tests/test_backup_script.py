"""Tests for the deployed memory-tree backup script (narada/.smriti/backup.py)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "narada" / ".smriti" / "backup.py"


@pytest.fixture()
def backup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_backup_script", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (tmp_path / ".git" / "objects").mkdir()
    return tmp_path


def _lock(path: Path, age_s: float) -> Path:
    path.write_text("", encoding="utf-8")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def _log(repo: Path) -> list[dict[str, object]]:
    path = repo / ".git" / "smriti-backup.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_stale_locks_are_cleared_when_no_git_runs(backup: ModuleType, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    index = _lock(repo / ".git" / "index.lock", 3600)
    ref = _lock(repo / ".git" / "refs" / "remotes" / "origin" / "master.lock", 3600)
    fresh = _lock(repo / ".git" / "HEAD.lock", 10)
    monkeypatch.setattr(backup, "git_running", lambda: False)

    backup.clear_stale_locks(repo)

    assert not index.exists() and not ref.exists()
    assert fresh.exists()  # younger than LOCK_STALE_S: may belong to a live git
    event = _log(repo)[-1]
    assert event["event"] == "stale_lock_cleared"
    assert set(event["locks"]) == {".git/index.lock", ".git/refs/remotes/origin/master.lock"}


def test_stale_locks_are_kept_while_git_runs(backup: ModuleType, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    index = _lock(repo / ".git" / "index.lock", 3600)
    monkeypatch.setattr(backup, "git_running", lambda: True)

    backup.clear_stale_locks(repo)

    assert index.exists()
    assert _log(repo)[-1]["event"] == "stale_lock_kept"


def test_no_locks_means_no_log_line(backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    backup.clear_stale_locks(repo)
    assert not (repo / ".git" / "smriti-backup.jsonl").exists()


def test_timeout_leaves_the_process_running(backup: ModuleType, tmp_path: Path) -> None:
    done = tmp_path / "done"
    code, msg = backup.run_cmd(
        [sys.executable, "-c",
         f"import time; time.sleep(2); open({str(done)!r}, 'w').write('x')"],
        tmp_path, timeout=0.3,
    )
    assert code == 124 and "left to finish" in msg
    deadline = time.time() + 15
    while not done.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert done.exists(), "the timed-out process was killed"


def test_run_cmd_returns_output_and_code(backup: ModuleType, tmp_path: Path) -> None:
    code, out = backup.run_cmd(
        [sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], tmp_path, timeout=30,
    )
    assert code == 3 and out == "hi"


def test_log_is_bounded(backup: ModuleType, tmp_path: Path,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(backup, "LOG_MAX_BYTES", 2_000)
    monkeypatch.setattr(backup, "LOG_KEEP_LINES", 10)
    for i in range(100):
        backup.log_event(repo, event="push", n=i)
    lines = _log(repo)
    assert len(lines) <= 30 and lines[-1]["n"] == 99

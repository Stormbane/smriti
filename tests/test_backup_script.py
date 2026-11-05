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


def test_a_lock_replaced_after_inspection_is_not_deleted(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    old = _lock(repo / ".git" / "index.lock", 3600)
    seen = backup.stale_locks(repo)
    # A live git removes the stale lock and takes a fresh one before we act.
    old.unlink()
    (repo / ".git" / "index.lock").write_text("live", encoding="utf-8")
    monkeypatch.setattr(backup, "stale_locks", lambda cwd: seen)
    monkeypatch.setattr(backup, "git_running", lambda: False)

    backup.clear_stale_locks(repo)

    assert (repo / ".git" / "index.lock").read_text(encoding="utf-8") == "live"


@pytest.mark.parametrize("returncode,stdout,expect", [
    (0, '"git.exe","1234","Console","1","50,000 K"\n', True),
    (0, "INFO: No tasks are running which match the specified criteria.\n", False),
    (1, "", True),  # the check itself failed: unknown counts as running
])
def test_git_running_trusts_only_a_successful_listing(
        backup: ModuleType, monkeypatch: pytest.MonkeyPatch,
        returncode: int, stdout: str, expect: bool) -> None:
    import subprocess

    monkeypatch.setattr(backup.os, "name", "nt")
    monkeypatch.setattr(backup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode, stdout=stdout, stderr=""))
    assert backup.git_running() is expect


@pytest.mark.parametrize("returncode,expect", [(0, True), (1, False), (2, True), (3, True)])
def test_pgrep_status_other_than_none_found_counts_as_running(
        backup: ModuleType, monkeypatch: pytest.MonkeyPatch, returncode: int, expect: bool) -> None:
    import subprocess

    monkeypatch.setattr(backup.os, "name", "posix")
    monkeypatch.setattr(backup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], returncode, stdout=b"", stderr=b""))
    assert backup.git_running() is expect


def test_run_lock_is_exclusive_and_released_only_by_its_owner(
        backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    token = backup.acquire_run_lock(repo)
    assert token is not None
    assert backup.acquire_run_lock(repo) is None  # held by this live process
    backup.release_run_lock(repo, "someone else's token")
    assert (repo / ".git" / backup.RUN_LOCK_NAME).exists()
    backup.release_run_lock(repo, token)
    assert not (repo / ".git" / backup.RUN_LOCK_NAME).exists()


def test_run_lock_of_a_dead_owner_is_broken(backup: ModuleType, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / backup.RUN_LOCK_NAME).write_text("999999 deadbeef", encoding="ascii")
    monkeypatch.setattr(backup, "pid_alive", lambda pid: False)
    assert backup.acquire_run_lock(repo) is not None


def test_run_lock_of_a_live_owner_is_not_broken_by_age_alone(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    _lock(repo / ".git" / backup.RUN_LOCK_NAME, 3600).write_text("4242 cafe", encoding="ascii")
    old = time.time() - 3600
    os.utime(repo / ".git" / backup.RUN_LOCK_NAME, (old, old))
    monkeypatch.setattr(backup, "pid_alive", lambda pid: True)
    assert backup.acquire_run_lock(repo) is None


def test_a_fresh_empty_run_lock_counts_as_busy(backup: ModuleType, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # Another backup has created the lock but not yet written its token.
    repo = _repo(tmp_path)
    (repo / ".git" / backup.RUN_LOCK_NAME).write_text("", encoding="ascii")
    monkeypatch.setattr(backup, "pid_alive", lambda pid: False)
    assert backup.acquire_run_lock(repo) is None
    assert (repo / ".git" / backup.RUN_LOCK_NAME).exists()


def test_an_old_empty_run_lock_is_broken(backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _lock(repo / ".git" / backup.RUN_LOCK_NAME, backup.RUN_LOCK_INIT_GRACE_S + 60)
    assert backup.acquire_run_lock(repo) is not None


def test_the_lock_never_exists_without_its_token(backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    token = backup.acquire_run_lock(repo)
    assert token is not None
    assert (repo / ".git" / backup.RUN_LOCK_NAME).read_text(encoding="ascii") == token
    assert not list((repo / ".git").glob(f"{backup.RUN_LOCK_NAME}.*.tmp"))


def test_run_lock_works_in_a_linked_worktree(backup: ModuleType, tmp_path: Path) -> None:
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not available")
    main, linked = tmp_path / "main", tmp_path / "linked"
    main.mkdir()

    def git(*args: str, cwd: Path = main) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "init")
    git("worktree", "add", "-q", str(linked))
    assert (linked / ".git").is_file()

    token = backup.acquire_run_lock(linked)
    assert token is not None
    assert (main / ".git" / backup.RUN_LOCK_NAME).exists()  # shared across worktrees
    assert backup.acquire_run_lock(main) is None
    backup.release_run_lock(linked, token)
    assert not (main / ".git" / backup.RUN_LOCK_NAME).exists()


@pytest.mark.parametrize("without_psutil", [False, True])
def test_pid_alive_sees_this_process_and_not_a_finished_one(
        backup: ModuleType, monkeypatch: pytest.MonkeyPatch, without_psutil: bool) -> None:
    import subprocess

    if without_psutil:  # exercise the ctypes / os.kill fallback that fresh installs use
        monkeypatch.setitem(sys.modules, "psutil", None)
    assert backup.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not backup.pid_alive(proc.pid)


def _real_repo(tmp_path: Path) -> Path:
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = tmp_path / "tree"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "note.md").write_text("hello\n", encoding="utf-8")
    return repo


def _commits(repo: Path) -> int:
    import subprocess

    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                         capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


def _run_script(repo: Path, *args: str) -> float:
    import subprocess

    env = {**os.environ, "SMRITI_ROOT": str(repo)}
    env.pop("SMRITI_INTERNAL", None)
    start = time.monotonic()
    subprocess.run([sys.executable, str(_SCRIPT), "--tag", "t", *args], env=env, check=True,
                   timeout=120)
    return time.monotonic() - start


def test_default_run_detaches_and_the_worker_still_commits(tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    _run_script(repo)
    deadline = time.time() + 60
    while _commits(repo) < 2 and time.time() < deadline:
        time.sleep(0.5)
    assert _commits(repo) == 2, "the detached worker never committed"
    while (repo / ".git" / "smriti-backup.lock").exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not (repo / ".git" / "smriti-backup.lock").exists()


def test_foreground_commits_before_returning(tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    _run_script(repo, "--foreground")
    assert _commits(repo) == 2


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
    # Bounded in bytes, so the line count depends on the platform's newline.
    size = (backup.git_dirs(repo)[1] / backup.LOG_NAME).stat().st_size
    assert size <= 2_000 + 100 and len(lines) >= 10 and lines[-1]["n"] == 99

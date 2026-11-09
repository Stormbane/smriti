"""Tests for one backup run of the memory tree (``python -m smriti.backup``)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import smriti.backup.run as backup_run


@pytest.fixture()
def backup() -> ModuleType:
    return backup_run


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


# The run lock: an OS lock held for the run, never a file whose
# existence is the lock. "Held" below is a RunLock; a refusal is its reason.


def _holds(backup: ModuleType, result: object) -> bool:
    return isinstance(result, getattr(backup, "RunLock", ()))


def _identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_ino, st.st_size


def test_a_second_contender_is_refused_while_the_first_holds_the_lock(
        backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = backup.acquire_run_lock(repo)
    assert _holds(backup, first)
    assert backup.acquire_run_lock(repo) == backup.RUN_LOCK_BUSY
    backup.release_run_lock(first)
    again = backup.acquire_run_lock(repo)
    assert _holds(backup, again)
    backup.release_run_lock(again)


def test_a_live_holder_is_never_taken_over_however_old_its_lock(
        backup: ModuleType, tmp_path: Path) -> None:
    """The old lock was taken from a live owner after RUN_LOCK_MAX_AGE_S."""
    repo = _repo(tmp_path)
    holder = backup.acquire_run_lock(repo)
    path = repo / ".git" / backup.RUN_LOCK_NAME
    old = time.time() - backup.RUN_LOCK_MAX_AGE_S - 3600
    os.utime(path, (old, old))
    seen = _identity(path)
    contender = backup.acquire_run_lock(repo)
    assert _identity(path) == seen  # not deleted and replaced
    assert not _holds(backup, contender)
    backup.release_run_lock(holder)


def test_a_contender_that_judges_the_holder_dead_still_cant_take_its_lock(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check-then-unlink interleaving: a contender read a dead owner's token,
    another run took the lock, and the contender's unlink deleted the new owner's.
    Whatever a contender believes about the holder, the OS lock stands."""
    repo = _repo(tmp_path)
    holder = backup.acquire_run_lock(repo)
    path = repo / ".git" / backup.RUN_LOCK_NAME
    seen = _identity(path)
    monkeypatch.setattr(backup, "pid_alive", lambda pid: False)
    contender = backup.acquire_run_lock(repo)
    assert _identity(path) == seen
    assert not _holds(backup, contender)
    backup.release_run_lock(holder)


def test_no_contender_ever_deletes_the_lock_file(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    path = repo / ".git" / backup.RUN_LOCK_NAME
    path.write_text("999999 deadbeef", encoding="ascii")  # a crashed older script's token
    monkeypatch.setattr(backup, "pid_alive", lambda pid: False)
    deleted: list[str] = []
    real_unlink = os.unlink

    def unlink(target: Any, *args: Any, **kwargs: Any) -> None:
        deleted.append(os.path.basename(os.fspath(target)))
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "remove", unlink)
    first = backup.acquire_run_lock(repo)  # takes the crashed run's lock without a delete
    assert backup.RUN_LOCK_NAME not in deleted
    assert _holds(backup, first)
    assert not _holds(backup, backup.acquire_run_lock(repo))
    backup.release_run_lock(first)
    assert backup.RUN_LOCK_NAME not in deleted
    assert path.exists()  # kept after release: the next run takes the same file


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows can't delete a file another handle has open")
def test_a_lock_file_deleted_before_it_was_locked_is_let_go(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An older script deletes a lock file it judged dead. A run that opened the
    file before that delete must not go on holding a lock on a file no path names,
    while the next run creates another and locks that."""
    repo = _repo(tmp_path)
    path = repo / ".git" / backup.RUN_LOCK_NAME
    real_lock = backup._lock
    calls: list[int] = []

    def lock_after_a_delete(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 1:
            path.unlink()
        real_lock(fd)

    monkeypatch.setattr(backup, "_lock", lock_after_a_delete)
    lock = backup.acquire_run_lock(repo)
    assert _holds(backup, lock) and len(calls) == 2
    assert os.fstat(lock.fd).st_ino == path.stat().st_ino
    monkeypatch.setattr(backup, "_lock", real_lock)
    assert backup.acquire_run_lock(repo) == backup.RUN_LOCK_BUSY
    backup.release_run_lock(lock)


_HOLDER = """
import sys, time
from pathlib import Path
from smriti.backup.run import acquire_run_lock
lock = acquire_run_lock(Path(sys.argv[1]))
print("busy" if isinstance(lock, str) else "held", flush=True)
time.sleep(120)
"""


def test_a_lock_whose_holder_died_is_taken(backup: ModuleType, tmp_path: Path) -> None:
    """The OS drops a crashed holder's lock: no token to judge, nothing to break."""
    import subprocess

    repo = _repo(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(repo)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        assert backup.acquire_run_lock(repo) == backup.RUN_LOCK_BUSY  # a live holder
    finally:
        proc.kill()
        proc.wait()
    # Windows drops a dead process's locks soon after it ends, not at once.
    deadline = time.monotonic() + 10
    lock = backup.acquire_run_lock(repo)
    while not _holds(backup, lock) and time.monotonic() < deadline:
        time.sleep(0.1)
        lock = backup.acquire_run_lock(repo)
    assert _holds(backup, lock)
    backup.release_run_lock(lock)


@pytest.mark.parametrize(("alive", "age_s", "busy"), [
    (True, 60, True),  # an older script's run still going as this one is deployed
    (False, 60, False),  # a crashed run's token
    (True, 6 * 3600 + 60, False),  # its pid alive after six hours: reused
])
def test_an_older_scripts_token_is_honoured_while_its_run_goes(
        backup: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        alive: bool, age_s: float, busy: bool) -> None:
    """An older backup.py's lock was the file holding "<pid> <hex>"; such a run
    overlapping the upgrade still excludes this one."""
    repo = _repo(tmp_path)
    (repo / ".git" / backup.RUN_LOCK_NAME).write_text("4242 cafe", encoding="ascii")
    old = time.time() - age_s
    os.utime(repo / ".git" / backup.RUN_LOCK_NAME, (old, old))
    monkeypatch.setattr(backup, "pid_alive", lambda pid: alive)
    lock = backup.acquire_run_lock(repo)
    if busy:
        assert lock == backup.RUN_LOCK_BUSY
        assert (repo / ".git" / backup.RUN_LOCK_NAME).read_text(encoding="ascii") == "4242 cafe"
    else:
        assert _holds(backup, lock)
        backup.release_run_lock(lock)


def test_a_holders_token_reads_as_a_live_owner_to_an_older_script(
        backup: ModuleType, tmp_path: Path) -> None:
    """The older script reads the file's first field as its owner's pid. On
    Windows it can't read the locked file at all, which it takes as busy too."""
    repo = _repo(tmp_path)
    lock = backup.acquire_run_lock(repo)
    assert _holds(backup, lock)
    os.lseek(lock.fd, 0, os.SEEK_SET)
    fields = os.read(lock.fd, 256).decode("ascii").split()
    assert fields[0] == str(os.getpid()) and fields[2] == backup.RUN_LOCK_KIND
    backup.release_run_lock(lock)


def test_a_lock_that_cant_be_opened_skips_the_run_and_says_why(
        backup: ModuleType, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / backup.RUN_LOCK_NAME).mkdir()  # a directory can't be opened as the lock
    reason = backup.acquire_run_lock(repo)
    assert isinstance(reason, str) and reason.startswith("the run lock can't be opened")


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

    lock = backup.acquire_run_lock(linked)
    assert _holds(backup, lock)
    assert (main / ".git" / backup.RUN_LOCK_NAME).exists()  # shared across worktrees
    assert backup.acquire_run_lock(main) == backup.RUN_LOCK_BUSY
    backup.release_run_lock(lock)
    second = backup.acquire_run_lock(main)
    assert _holds(backup, second)
    backup.release_run_lock(second)


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


def _run(repo: Path, *args: str) -> int:
    """``python -m smriti.backup`` on *repo*, as trigger starts it; its exit code."""
    import subprocess

    env = {**os.environ, "SMRITI_ROOT": str(repo)}
    env.pop("SMRITI_INTERNAL", None)
    return subprocess.run([sys.executable, "-P", "-m", "smriti.backup", "--root", str(repo),
                           "--tag", "t", *args], env=env, timeout=120).returncode


def test_a_run_commits_before_returning_and_lets_go_of_its_lock(
        backup: ModuleType, tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    assert _run(repo) == 0
    assert _commits(repo) == 2
    lock = backup.acquire_run_lock(repo)
    assert _holds(backup, lock)
    backup.release_run_lock(lock)
    assert json.loads((repo / ".git" / "smriti-backup.jsonl").read_text(
        encoding="utf-8").splitlines()[-1])["event"] == "commit"


def test_a_run_takes_the_memory_root_when_none_is_given(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _real_repo(tmp_path)
    monkeypatch.setenv("SMRITI_ROOT", str(repo))
    assert backup_run.main(["--tag", "t"]) == 0
    assert _commits(repo) == 2


def test_a_tree_that_is_no_repository_is_left_alone(tmp_path: Path) -> None:
    assert backup_run.main(["--root", str(tmp_path), "--tag", "t"]) == 0
    assert list(tmp_path.iterdir()) == []


def test_a_run_skipped_for_another_backup_logs_why(backup: ModuleType, tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    held = backup.acquire_run_lock(repo)
    try:
        assert backup_run.main(["--root", str(repo), "--tag", "t"]) == 0
    finally:
        backup.release_run_lock(held)
    assert _commits(repo) == 1
    assert _log(repo)[-1] | {"at": ""} == {"at": "", "event": "skipped", "tag": "t",
                                           "reason": backup.RUN_LOCK_BUSY}


def test_push_happens_only_when_there_is_something_new(tmp_path: Path) -> None:
    import subprocess

    repo = _real_repo(tmp_path)
    remote = tmp_path / "remote.git"
    for args, cwd in ((["init", "-q", "--bare", str(remote)], tmp_path),
                      (["branch", "-M", "master"], repo),
                      (["remote", "add", "origin", str(remote)], repo)):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    _run(repo, "--push")  # commits note.md, pushes
    _run(repo, "--push")  # nothing new: no network call
    pushes = [e for e in _log(repo) if e.get("event") == "push"]
    assert [p["ok"] for p in pushes] == [True]

    (repo / "note.md").write_text("changed\n", encoding="utf-8")
    _run(repo, "--push")
    pushes = [e for e in _log(repo) if e.get("event") == "push"]
    assert [p["ok"] for p in pushes] == [True, True]


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _main_repo(tmp_path: Path, *, upstream: str | None = "main") -> tuple[Path, Path]:
    """A repo on ``main`` with a bare ``origin``; tracking ``origin/<upstream>`` unless None."""
    repo = _real_repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    if upstream is not None:
        _git(repo, "push", "-q", "-u", "origin", f"main:{upstream}")
    return repo, remote


def _pushes(repo: Path) -> list[dict[str, object]]:
    return [e for e in _log(repo) if e.get("event") == "push"]


def test_a_main_branch_is_pushed_to_its_upstream(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path)
    _run(repo, "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--count", "@{u}..HEAD") == "0"


def test_a_main_branch_with_nothing_new_is_not_pushed(tmp_path: Path) -> None:
    repo, _ = _main_repo(tmp_path)
    _run(repo, "--push")
    _run(repo, "--push")  # nothing new: no push at all
    assert [p["ok"] for p in _pushes(repo)] == [True]


def test_the_upstream_is_honoured_when_its_name_differs(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path, upstream="trunk")
    _run(repo, "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "trunk") == _git(repo, "rev-parse", "HEAD")
    assert _git(remote, "branch", "--list", "main") == ""  # never a same-named guess


def test_a_branch_without_an_upstream_gets_one_on_its_first_push(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path, upstream=None)
    _run(repo, "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "--abbrev-ref", "main@{u}") == "origin/main"
    _run(repo, "--push")
    assert len(_pushes(repo)) == 1  # and then compares against it


def test_a_detached_head_is_not_pushed_and_says_why(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path)
    before = _git(remote, "rev-parse", "main")
    _git(repo, "checkout", "-q", "--detach")
    _run(repo, "--push")
    assert _commits(repo) == 2  # the commit still happens
    assert _pushes(repo) == []
    skipped = [e for e in _log(repo) if e.get("event") == "push_skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "HEAD is detached"
    assert _git(remote, "rev-parse", "main") == before


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


def test_log_is_bounded(backup: ModuleType, tmp_path: Path,
                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(backup, "LOG_MAX_BYTES", 2_000)
    monkeypatch.setattr(backup, "LOG_KEEP_LINES", 10)
    for i in range(100):
        backup.log_event(repo, event="push", n=i)
    lines = _log(repo)
    # Bounded in bytes, so the line count depends on the platform's newline.
    size = (repo / ".git" / backup.LOG_NAME).stat().st_size
    assert size <= 2_000 + 100 and len(lines) >= 10 and lines[-1]["n"] == 99

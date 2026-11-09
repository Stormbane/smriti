"""Tests for the deployed memory-tree backup script (narada/.smriti/backup.py)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

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


# The run lock (H15.6): an OS lock held for the run, never a file whose
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
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("held_backup", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
lock = mod.acquire_run_lock(Path(sys.argv[2]))
print("busy" if isinstance(lock, str) else "held", flush=True)
time.sleep(120)
"""


def test_a_lock_whose_holder_died_is_taken(backup: ModuleType, tmp_path: Path) -> None:
    """The OS drops a crashed holder's lock: no token to judge, nothing to break."""
    import subprocess

    repo = _repo(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(_SCRIPT), str(repo)],
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
    """Before H15.6 the lock was the file holding "<pid> <hex>"; such a run
    overlapping the upgrade still excludes the new script."""
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


def test_default_run_detaches_and_the_worker_still_commits(backup: ModuleType,
                                                           tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    _run_script(repo)
    deadline = time.time() + 60
    while _commits(repo) < 2 and time.time() < deadline:
        time.sleep(0.5)
    assert _commits(repo) == 2, "the detached worker never committed"
    lock = backup.acquire_run_lock(repo)  # the worker let go of the run lock
    while not _holds(backup, lock) and time.time() < deadline:
        time.sleep(0.2)
        lock = backup.acquire_run_lock(repo)
    assert _holds(backup, lock)
    backup.release_run_lock(lock)


def test_foreground_commits_before_returning(tmp_path: Path) -> None:
    repo = _real_repo(tmp_path)
    _run_script(repo, "--foreground")
    assert _commits(repo) == 2


def test_push_happens_only_when_there_is_something_new(tmp_path: Path) -> None:
    import subprocess

    repo = _real_repo(tmp_path)
    remote = tmp_path / "remote.git"
    for args, cwd in ((["init", "-q", "--bare", str(remote)], tmp_path),
                      (["branch", "-M", "master"], repo),
                      (["remote", "add", "origin", str(remote)], repo)):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    _run_script(repo, "--foreground", "--push")  # commits note.md, pushes
    _run_script(repo, "--foreground", "--push")  # nothing new: no network call
    pushes = [e for e in _log(repo) if e.get("event") == "push"]
    assert [p["ok"] for p in pushes] == [True]

    (repo / "note.md").write_text("changed\n", encoding="utf-8")
    _run_script(repo, "--foreground", "--push")
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
    _run_script(repo, "--foreground", "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--count", "@{u}..HEAD") == "0"


def test_a_main_branch_with_nothing_new_is_not_pushed(tmp_path: Path) -> None:
    repo, _ = _main_repo(tmp_path)
    _run_script(repo, "--foreground", "--push")
    _run_script(repo, "--foreground", "--push")  # nothing new: no push at all
    assert [p["ok"] for p in _pushes(repo)] == [True]


def test_the_upstream_is_honoured_when_its_name_differs(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path, upstream="trunk")
    _run_script(repo, "--foreground", "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "trunk") == _git(repo, "rev-parse", "HEAD")
    assert _git(remote, "branch", "--list", "main") == ""  # never a same-named guess


def test_a_branch_without_an_upstream_gets_one_on_its_first_push(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path, upstream=None)
    _run_script(repo, "--foreground", "--push")
    assert [p["ok"] for p in _pushes(repo)] == [True]
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "--abbrev-ref", "main@{u}") == "origin/main"
    _run_script(repo, "--foreground", "--push")
    assert len(_pushes(repo)) == 1  # and then compares against it


def test_a_detached_head_is_not_pushed_and_says_why(tmp_path: Path) -> None:
    repo, remote = _main_repo(tmp_path)
    before = _git(remote, "rev-parse", "main")
    _git(repo, "checkout", "-q", "--detach")
    _run_script(repo, "--foreground", "--push")
    assert _commits(repo) == 2  # the commit still happens
    assert _pushes(repo) == []
    skipped = [e for e in _log(repo) if e.get("event") == "push_skipped"]
    assert len(skipped) == 1 and "detached HEAD" in str(skipped[0]["reason"])
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

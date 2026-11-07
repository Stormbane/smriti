"""The OS-level exclusive lock: exclusive across opens, released on exit.

A named lock is one file in the private runtime directory, and fails closed
when that directory can't be used (H11.9, H14.8).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import smriti
from smriti.core import oslock
from smriti.core.oslock import exclusive_lock, lock_path


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runtime directory of its own, and a temp directory to show nothing lands there."""
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(shared))
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(oslock, "_warned", set())
    return shared


@contextmanager
def _held(key: oslock.LockKey | Path) -> Iterator[None]:
    with exclusive_lock(key, wait_s=1) as held:
        assert held
        yield


def _free(path: Path) -> bool:
    """Whether a plain lock on ``path`` can be taken right now."""
    with exclusive_lock(path, wait_s=0) as held:
        return held


# ------------------------------------------------------------------ basics


def test_second_holder_waits_then_gives_up(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with exclusive_lock(path) as first:
        assert first
        start = time.monotonic()
        with exclusive_lock(path, wait_s=0.3) as second:
            assert not second
        assert time.monotonic() - start >= 0.25


def test_released_lock_can_be_taken_again(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with exclusive_lock(path) as held:
        assert held
    with exclusive_lock(path, wait_s=0.1) as again:
        assert again


def test_unopenable_lock_path_yields_false(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a dir", encoding="utf-8")
    with exclusive_lock(blocker / "x.lock", wait_s=0.1) as held:
        assert not held


def test_try_lock_says_whether_a_lock_is_busy_or_cannot_be_had(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    blocker = tmp_path / "file"
    blocker.write_text("not a dir", encoding="utf-8")
    with oslock.try_lock(path) as first:
        assert first == oslock.HELD
        with oslock.try_lock(path, wait_s=0.1) as second:
            assert second == oslock.BUSY
    with oslock.try_lock(blocker / "x.lock", wait_s=0.1) as nothing:
        assert nothing == oslock.UNAVAILABLE
    with oslock.try_lock(path, wait_s=0.1) as again:
        assert again == oslock.HELD


def test_lock_path_is_outside_the_tree_and_stable(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    a = lock_path("handoff", str(tree))
    assert a == lock_path("handoff", str(tree))
    assert tree not in a.private().parents


def test_two_holders_of_a_named_lock_exclude_each_other(tmp_path: Path) -> None:
    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        with exclusive_lock(key, wait_s=0.3) as second:
            assert not second
    with exclusive_lock(key, wait_s=0.3) as again:
        assert again


# -------------------------------------------------------------- where


def test_a_named_lock_is_one_file_in_the_runtime_directory(
        tmp_path: Path, _isolated_locks: Path) -> None:
    from smriti.core.rundir import runtime_dir

    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        assert key.private().parent == runtime_dir() / "locks"
        assert not _free(key.private())
    assert _free(key.private())
    assert list(_isolated_locks.iterdir()) == [], "nothing in the shared temp directory"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows relies on ACLs")
def test_private_lock_files_are_the_owners_alone(tmp_path: Path) -> None:
    from smriti.core.rundir import runtime_dir

    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        pass
    locks = runtime_dir() / "locks"
    assert key.private().parent == locks
    assert locks.stat().st_mode & 0o077 == 0
    assert runtime_dir().stat().st_mode & 0o077 == 0
    assert key.private().stat().st_mode & 0o077 == 0
    assert key.private().stat().st_uid == os.getuid()


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of its own, and no SMRITI_RUNTIME_DIR: the default runtime directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("SMRITI_RUNTIME_DIR", raising=False)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    return home


def test_the_runtime_directory_does_not_follow_xdg_runtime_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cron job has no XDG_RUNTIME_DIR where a desktop session has one: both
    resolve one directory, so they take the same lock files."""
    from smriti.core.rundir import default_runtime_dir

    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")  # the POSIX rule, on every OS
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    without = default_runtime_dir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    assert default_runtime_dir() == without == home / ".cache" / "smriti" / "run"


def test_a_process_without_xdg_runtime_dir_excludes_one_with_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    target = tmp_path / "tree" / "journal.md"
    target.parent.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    with _process_holding("edit", target):  # a desktop session's process
        monkeypatch.delenv("XDG_RUNTIME_DIR")  # this one: a cron job
        assert not _try_in_this_process("edit", target)
    assert _try_in_this_process("edit", target)


def test_contending_takers_complete_and_never_overlap(tmp_path: Path) -> None:
    """Two takers each take two named locks in opposite orders, in a loop; both
    finish, one holder at a time."""
    k1 = lock_path("edit", str(tmp_path / "a"))
    k2 = lock_path("edit", str(tmp_path / "b"))
    inside = {k1: 0, k2: 0}
    worst = {k1: 0, k2: 0}
    failures: list[str] = []
    guard = threading.Lock()

    def visit(key: oslock.LockKey) -> None:
        with exclusive_lock(key, wait_s=20) as held:
            if not held:
                failures.append(f"{key.scope} not taken")
                return
            with guard:
                inside[key] += 1
                worst[key] = max(worst[key], inside[key])
            time.sleep(0.001)
            with guard:
                inside[key] -= 1

    def taker(order: list[oslock.LockKey]) -> None:
        for _ in range(40):
            for key in order:
                visit(key)

    threads = [threading.Thread(target=taker, args=([k1, k2],)),
               threading.Thread(target=taker, args=([k2, k1],))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a taker never finished"
    assert failures == []
    assert worst == {k1: 1, k2: 1}


# --------------------------------------------------------- fail closed


def _refuse_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.core.rundir as runtime

    def refuse(*, create: bool = True) -> Path:
        raise runtime.RuntimeDirError("refusing the runtime directory: other users can write")

    monkeypatch.setattr(runtime, "runtime_dir", refuse)


def test_a_refused_runtime_directory_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_locks: Path,
        caplog: pytest.LogCaptureFixture) -> None:
    # Others hold the file in the runtime directory and no other: a lock
    # anywhere else would be a lock of its own.
    key = lock_path("edit", str(tmp_path / "tree"))
    private = key.private()
    with _held(private):  # another process, inside the critical section
        _refuse_runtime_dir(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="smriti.core.oslock"):
            with oslock.try_lock(key, wait_s=0.3) as outcome:
                assert outcome == oslock.UNAVAILABLE
            with exclusive_lock(key, wait_s=0.3) as held:
                assert not held
    warnings = [r for r in caplog.records if "can't be taken" in r.getMessage()]
    assert len(warnings) == 1, "warned once, not on every lock"
    with exclusive_lock(key, wait_s=0.3) as alone:
        assert not alone  # nobody holds it now, and still: never another file
    assert list(_isolated_locks.iterdir()) == [], "no fallback to the shared temp directory"


def test_a_runtime_directory_that_cant_be_made_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(blocker / "run"))
    key = lock_path("edit", str(tmp_path / "tree"))
    with oslock.try_lock(key, wait_s=0.3) as outcome:
        assert outcome == oslock.UNAVAILABLE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_a_world_writable_runtime_directory_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "open-run"
    run.mkdir()
    run.chmod(0o777)
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(run))
    key = lock_path("edit", str(tmp_path / "tree"))
    with oslock.try_lock(key, wait_s=0.3) as outcome:
        assert outcome == oslock.UNAVAILABLE
    assert not (run / "locks").exists(), "nothing created in a refused directory"


# ------------------------------------------------- separate processes

_HOLDER = """
import sys
from pathlib import Path

kind, target = sys.argv[1], Path(sys.argv[2])
if kind == "edit":
    from smriti.core.files import edit_lock
    held = edit_lock(target, wait_s=10)
else:
    from smriti.daylog.lock import writer_lock
    held = writer_lock(target, timeout_s=10)
with held as ok:
    if ok is False:
        sys.exit("busy")
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextmanager
def _process_holding(kind: str, target: Path) -> Iterator[None]:
    env = dict(os.environ)
    src = str(Path(smriti.__file__).resolve().parents[1])  # the smriti this test imported
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, kind, str(target)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        assert proc.stdout.readline().strip() == "held"
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _try_in_this_process(kind: str, target: Path) -> bool:
    """Whether this process gets ``kind``'s lock on ``target`` now."""
    from smriti.core.files import FileBusy, edit_lock
    from smriti.daylog.lock import LockTimeout, writer_lock

    try:
        if kind == "edit":
            with edit_lock(target, wait_s=0.3):
                return True
        with writer_lock(target, timeout_s=0.3):
            return True
    except (FileBusy, LockTimeout):
        return False


@pytest.mark.parametrize("kind", ["edit", "writer"])
def test_two_processes_exclude_each_other(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "tree" / ("journal.md" if kind == "edit" else "daylog.lock")
    target.parent.mkdir()
    with _process_holding(kind, target):
        assert not _try_in_this_process(kind, target)
    assert _try_in_this_process(kind, target), "released when the holder let go"

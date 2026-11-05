"""The OS-level exclusive lock: exclusive across opens, released on exit.

Named locks moved from the shared temp directory to the private runtime
directory (H11.9) through a transition that takes both files, the private one
first. "Old code" below is what releases before the move ran: one file,
``<tempdir>/smriti-locks/<sha1(scope)[:16]>-<name>.lock``, locked alone.
"""
from __future__ import annotations

import hashlib
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
    """A temp directory standing in for the shared one, here and in child processes."""
    shared = tmp_path / "shared-tmp"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(shared))
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv(oslock.LEGACY_LOCKS_ENV, raising=False)
    monkeypatch.setattr(oslock, "_warned", set())
    return shared


def _old_lock_file(name: str, scope: str) -> Path:
    """Where old code locked: computed here, independently of oslock."""
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "smriti-locks" / f"{digest}-{name}.lock"


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


def test_lock_path_is_outside_the_tree_and_stable(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    a = lock_path("handoff", str(tree))
    assert a == lock_path("handoff", str(tree))
    assert tree not in a.private().parents and tree not in a.legacy.parents


# -------------------------------------------------------------- where


def test_the_shared_file_is_exactly_where_old_code_locks(tmp_path: Path) -> None:
    scope = str(tmp_path / "tree")
    assert lock_path("edit", scope).legacy == _old_lock_file("edit", scope)


def test_a_named_lock_is_taken_in_the_runtime_directory(tmp_path: Path) -> None:
    from smriti.embed.runtime import runtime_dir

    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        assert key.private().parent == runtime_dir() / "locks"
        assert not _free(key.private()) and not _free(key.legacy)  # both held
    assert _free(key.private()) and _free(key.legacy)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows relies on ACLs")
def test_private_lock_files_are_the_owners_alone(tmp_path: Path) -> None:
    from smriti.embed.runtime import runtime_dir

    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        pass
    locks = runtime_dir() / "locks"
    assert key.private().parent == locks
    assert locks.stat().st_mode & 0o077 == 0
    assert runtime_dir().stat().st_mode & 0o077 == 0
    assert key.private().stat().st_mode & 0o077 == 0
    assert key.private().stat().st_uid == os.getuid()


# --------------------------------------------------------- old and new


def test_a_new_lock_excludes_an_old_holder(tmp_path: Path) -> None:
    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(_old_lock_file(key.name, key.scope)):  # old code: the shared file alone
        with exclusive_lock(key, wait_s=0.3) as held:
            assert not held
        assert _free(key.private()), "a failed attempt holds nothing"


def test_an_old_lock_excludes_a_new_holder(tmp_path: Path) -> None:
    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        with exclusive_lock(_old_lock_file(key.name, key.scope), wait_s=0.3) as old:
            assert not old


@pytest.mark.parametrize("legacy", ["1", "0"])
def test_two_new_holders_exclude_each_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            legacy: str) -> None:
    monkeypatch.setenv(oslock.LEGACY_LOCKS_ENV, legacy)
    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(key):
        with exclusive_lock(key, wait_s=0.3) as second:
            assert not second


def test_dropping_the_shared_file_takes_only_the_private_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Why the transition exists: without the shared file, old code isn't excluded.
    monkeypatch.setenv(oslock.LEGACY_LOCKS_ENV, "0")
    key = lock_path("edit", str(tmp_path / "tree"))
    with _held(_old_lock_file(key.name, key.scope)):
        with exclusive_lock(key, wait_s=0.3) as held:
            assert held  # beside an old holder: the shared file wasn't taken


# ---------------------------------------------------------------- order


def test_the_private_file_is_taken_before_the_shared_one(tmp_path: Path) -> None:
    key = lock_path("edit", str(tmp_path / "tree"))
    results: list[bool] = []

    def take() -> None:
        with exclusive_lock(key, wait_s=10) as held:
            results.append(held)

    # An old holder has the shared file: a new taker waits holding the private one.
    with _held(key.legacy):
        t = threading.Thread(target=take)
        t.start()
        deadline = time.monotonic() + 5
        while _free(key.private()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _free(key.private())
    t.join(timeout=15)
    assert results == [True]

    # The private file is busy: a new taker waits without touching the shared one.
    results.clear()
    with _held(key.private()):
        t = threading.Thread(target=take)
        t.start()
        for _ in range(10):
            time.sleep(0.03)
            assert _free(key.legacy)
    t.join(timeout=15)
    assert results == [True]


def test_contending_takers_complete_and_never_overlap(tmp_path: Path) -> None:
    """Two new takers each take two named locks in opposite orders, in a loop,
    beside an old-code taker of both; all finish, one holder at a time."""
    k1 = lock_path("edit", str(tmp_path / "a"))
    k2 = lock_path("edit", str(tmp_path / "b"))
    inside = {k1: 0, k2: 0}
    worst = {k1: 0, k2: 0}
    failures: list[str] = []
    guard = threading.Lock()

    def visit(key: oslock.LockKey, lock: oslock.LockKey | Path) -> None:
        with exclusive_lock(lock, wait_s=20) as held:
            if not held:
                failures.append(f"{key.scope} not taken")
                return
            with guard:
                inside[key] += 1
                worst[key] = max(worst[key], inside[key])
            time.sleep(0.001)
            with guard:
                inside[key] -= 1

    def new(order: list[oslock.LockKey]) -> None:
        for _ in range(40):
            for key in order:
                visit(key, key)

    def old() -> None:
        for _ in range(40):
            for key in (k1, k2):
                visit(key, _old_lock_file(key.name, key.scope))

    threads = [threading.Thread(target=new, args=([k1, k2],)),
               threading.Thread(target=new, args=([k2, k1],)),
               threading.Thread(target=old)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a taker never finished"
    assert failures == []
    assert worst == {k1: 1, k2: 1}


# ------------------------------------------------------------- fallback


def test_a_refused_runtime_directory_falls_back_to_the_shared_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import smriti.embed.runtime as runtime

    def refuse(*, create: bool = True) -> Path:
        raise runtime.RuntimeDirError("refusing the runtime directory: other users can write")

    monkeypatch.setattr(runtime, "runtime_dir", refuse)
    monkeypatch.setenv(oslock.LEGACY_LOCKS_ENV, "0")  # even with the shared file dropped
    key = lock_path("edit", str(tmp_path / "tree"))
    with caplog.at_level(logging.WARNING, logger="smriti.core.oslock"):
        with exclusive_lock(key, wait_s=0.3) as held:
            assert held, "never fail to lock"
            assert not _free(key.legacy)
            with exclusive_lock(key, wait_s=0.3) as second:
                assert not second  # and it still excludes
        with exclusive_lock(key, wait_s=0.3):
            pass
    warnings = [r for r in caplog.records if "fall back" in r.getMessage()]
    assert len(warnings) == 1, "warned once, not on every lock"


def test_a_runtime_directory_that_cant_be_made_falls_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(blocker / "run"))
    key = lock_path("edit", str(tmp_path / "tree"))
    with exclusive_lock(key, wait_s=0.3) as held:
        assert held
        assert not _free(key.legacy)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_a_world_writable_runtime_directory_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "open-run"
    run.mkdir()
    run.chmod(0o777)
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(run))
    key = lock_path("edit", str(tmp_path / "tree"))
    with exclusive_lock(key, wait_s=0.3) as held:
        assert held
        assert not _free(key.legacy)
    assert not (run / "locks").exists(), "nothing created in a refused directory"


def test_an_unusable_shared_file_on_posix_leaves_the_private_lock(
        tmp_path: Path, _isolated_locks: Path) -> None:
    # Another user's file where the shared directory should be: on POSIX no old
    # process of this user can lock there either. On Windows the temp directory
    # is the user's own, so a failure proves nothing and the lock isn't taken.
    (_isolated_locks / "smriti-locks").write_text("", encoding="utf-8")
    key = lock_path("edit", str(tmp_path / "tree"))
    with exclusive_lock(key, wait_s=0.3) as held:
        assert held is (sys.platform != "win32")
        if held:
            with exclusive_lock(key, wait_s=0.3) as second:
                assert not second


# ------------------------------------------- old and new processes, for real

_HOLDER = """
import hashlib, os, sys, tempfile
from pathlib import Path

role, kind, target = sys.argv[1], sys.argv[2], Path(sys.argv[3])
if role == "old":  # what releases before the move did: the shared file alone
    from smriti.core.oslock import exclusive_lock
    if kind == "edit":
        name, scope = "edit", os.path.normcase(str(target.resolve()))
    else:
        name, scope = "daylog-writer", str(target.resolve())
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:16]
    held = exclusive_lock(Path(tempfile.gettempdir()) / "smriti-locks" / f"{digest}-{name}.lock",
                          wait_s=10)
elif kind == "edit":
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
def _process_holding(role: str, kind: str, target: Path) -> Iterator[None]:
    env = dict(os.environ)
    src = str(Path(smriti.__file__).resolve().parents[1])  # the smriti this test imported
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, role, kind, str(target)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        assert proc.stdout.readline().strip() == "held"
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _try_in_this_process(role: str, kind: str, target: Path) -> bool:
    """Whether ``role`` code in this process gets ``kind``'s lock on ``target`` now."""
    from smriti.core.files import FileBusy, edit_lock
    from smriti.daylog.lock import LockTimeout, writer_lock

    if role == "old":
        name, scope = (("edit", os.path.normcase(str(target.resolve()))) if kind == "edit"
                       else ("daylog-writer", str(target.resolve())))
        with exclusive_lock(_old_lock_file(name, scope), wait_s=0.3) as held:
            return held
    try:
        if kind == "edit":
            with edit_lock(target, wait_s=0.3):
                return True
        with writer_lock(target, timeout_s=0.3):
            return True
    except (FileBusy, LockTimeout):
        return False


@pytest.mark.parametrize("kind", ["edit", "writer"])
@pytest.mark.parametrize("holder,taker", [("old", "new"), ("new", "old"), ("new", "new")])
def test_old_and_new_processes_exclude_each_other(tmp_path: Path, kind: str, holder: str,
                                                  taker: str) -> None:
    target = tmp_path / "tree" / ("journal.md" if kind == "edit" else "daylog.lock")
    target.parent.mkdir()
    with _process_holding(holder, kind, target):
        assert not _try_in_this_process(taker, kind, target)
    assert _try_in_this_process(taker, kind, target), "released when the holder let go"

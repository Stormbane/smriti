"""The OS-level exclusive lock: exclusive across opens, released on exit."""
from __future__ import annotations

import time
from pathlib import Path

from smriti.core.oslock import exclusive_lock, lock_path


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
    a = lock_path("handoff", str(tmp_path))
    assert a == lock_path("handoff", str(tmp_path))
    assert tmp_path not in a.parents

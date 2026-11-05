"""Concurrent edits to the memory tree: no writer may lose another's edit.

Each open session runs its own MCP server, so these races are real: two
sessions writing the same day's journal, or adding todos at once.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from smriti.core.files import FileBusy, create_new, edit_lock, write_atomic
from smriti.core.tree import tree_root


def _together(n: int, work: Callable[[int], object]) -> list[object]:
    """Run ``work(i)`` for i in range(n) on n threads released at once."""
    start = threading.Barrier(n)
    results: list[object] = [None] * n
    errors: list[BaseException] = []

    def run(i: int) -> None:
        start.wait()
        try:
            results[i] = work(i)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_parallel_entries_to_one_day_all_survive() -> None:
    from smriti.store.writer import write_entry

    root = tree_root()
    paths = _together(8, lambda i: write_entry(f"entry number {i}", branch="notes",
                                               title=f"t{i}", root=root, reindex=False))
    [path] = set(paths)
    text = Path(str(path)).read_text(encoding="utf-8")
    for i in range(8):
        assert f"entry number {i}" in text
    assert text.count("\n---\n\n---\n") == 7  # every join is one clean separator


def test_parallel_todos_all_land() -> None:
    from smriti.mcp_server import handle_add_todo

    todo = tree_root() / "mirrors" / "demo" / "ai" / "todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text("# TODO\n\n## Active\n", encoding="utf-8")
    _together(8, lambda i: handle_add_todo({
        "project": "demo", "topic": f"topic-{i}", "line": f"- [ ] topic-{i}", "kind": "build"}))
    text = todo.read_text(encoding="utf-8")
    assert all(f"topic-{i}" in text for i in range(8))


def test_racing_feature_opens_create_one_spec() -> None:
    from smriti.mcp_server import handle_record_feature

    (tree_root() / "mirrors" / "demo" / "ai").mkdir(parents=True)
    replies = _together(6, lambda i: handle_record_feature({
        "project": "demo", "slug": "same", "title": f"T{i}", "goal": "g", "plan": "p"}))
    opened = [r for r in replies if str(r).startswith("Opened")]
    assert len(opened) == 1, replies


def test_edit_lock_is_exclusive_and_says_busy(tmp_path: Path) -> None:
    target = tmp_path / "f.md"
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with edit_lock(target):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(FileBusy), edit_lock(target, wait_s=0.1):
            pass
    finally:
        release.set()
        t.join()
    with edit_lock(target, wait_s=1):
        pass


def test_write_atomic_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "f.md"
    target.write_text("old", encoding="utf-8")
    write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["f.md"]


def test_create_new_never_overwrites(tmp_path: Path) -> None:
    first = create_new(tmp_path / "2026-11-05-x.md", "one")
    second = create_new(tmp_path / "2026-11-05-x.md", "two")
    assert (first.name, second.name) == ("2026-11-05-x.md", "2026-11-05-x-2.md")
    assert first.read_text(encoding="utf-8") == "one"

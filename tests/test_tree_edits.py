"""Concurrent edits to the memory tree: no writer may lose another's edit.

Each open session runs its own MCP server, so these races are real: two
sessions writing the same day's journal, or adding todos at once.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from smriti.core.files import FileBusy, LockUnavailable, create_new, edit_lock, write_atomic
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
        except BaseException as exc:  # noqa: BLE001 — collected, asserted on the main thread
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
    from smriti.mcp.tools.projects import handle_add_todo

    todo = tree_root() / "mirrors" / "demo" / "ai" / "todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text("# TODO\n\n## Active\n", encoding="utf-8")
    _together(8, lambda i: handle_add_todo({
        "project": "demo", "topic": f"topic-{i}", "line": f"- [ ] topic-{i}", "kind": "build"}))
    text = todo.read_text(encoding="utf-8")
    assert all(f"topic-{i}" in text for i in range(8))


def test_racing_feature_opens_create_one_spec() -> None:
    from smriti.mcp.server import handle_message

    (tree_root() / "mirrors" / "demo" / "ai").mkdir(parents=True)
    replies = _together(6, lambda i: handle_message({
        "jsonrpc": "2.0", "id": i, "method": "tools/call",
        "params": {"name": "smriti_record_feature", "arguments": {
            "project": "demo", "slug": "same", "title": f"T{i}", "goal": "g", "plan": "p"}}}))
    opened = [r for r in replies if not r["result"].get("isError")]
    assert len(opened) == 1, replies


def test_same_title_same_day_findings_and_decisions_both_survive() -> None:
    from smriti.mcp.tools.projects import handle_record_decision, handle_record_finding

    mirror = tree_root() / "mirrors" / "demo"
    mirror.mkdir(parents=True)
    for n in (1, 2):
        handle_record_finding({"project": "demo", "title": "Same", "finding": f"finding {n}",
                               "why_it_matters": "w", "tags": ["locks"]})
        handle_record_decision({"project": "demo", "title": "Same", "decision": f"decision {n}",
                                "rationale": "r"})
    findings = sorted((mirror / "findings").glob("*.md"), key=lambda p: len(p.name))
    decisions = list((mirror / "decisions").glob("*.md"))
    assert [p.stem.split("-same")[-1] for p in findings] == ["", "-2"]
    assert "finding 1" in findings[0].read_text(encoding="utf-8")
    assert len(decisions) == 2
    assert {p.read_text(encoding="utf-8").count("decision ") for p in decisions} == {1}
    stubs = sorted((tree_root() / "semantic" / "concepts" / "cross-project" / "findings"
                    / "locks").glob("*.md"), key=lambda p: len(p.name))
    assert len(stubs) == 2
    assert f"[[mirrors/demo/findings/{findings[1].stem}]]" in stubs[1].read_text(encoding="utf-8")


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
        busy = pytest.raises(FileBusy, match="being edited elsewhere; try again")
        with busy, edit_lock(target, wait_s=0.1):
            pass
    finally:
        release.set()
        t.join()
    with edit_lock(target, wait_s=1):
        pass


def test_edit_lock_says_unavailable_when_no_lock_can_be_opened(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # H15.7: a refused runtime directory leaves no lock file to open, so no
    # writer is excluded and waiting won't help: not "busy, try again".
    import smriti.core.rundir as runtime

    def refuse(*, create: bool = True) -> Path:
        raise runtime.RuntimeDirError("refusing the runtime directory: other users can write")

    monkeypatch.setattr(runtime, "runtime_dir", refuse)
    target = tmp_path / "f.md"
    with pytest.raises(LockUnavailable) as raised, edit_lock(target, wait_s=0.1):
        pytest.fail("the edit ran without a lock")
    message = str(raised.value)
    assert not isinstance(raised.value, FileBusy) and isinstance(raised.value, OSError)
    why = "refusing the runtime directory: other users can write"
    assert f"edit lock is unavailable ({why})" in message and "SMRITI_RUNTIME_DIR" in message
    assert "try again" not in message and str(target) in message


def test_an_mcp_write_says_why_when_no_edit_lock_can_be_had(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.core.rundir as runtime
    from smriti.mcp.server import call_tool
    from smriti.mcp.tools.memory import TOOLS

    write = next(t for t in TOOLS if t.name == "smriti_write")
    args = {"content": "Ari met Sam at the harbour.", "branch": "journal"}
    first = call_tool(write, dict(args))  # the day's file exists; the next write appends
    assert not first.get("isError"), first

    def refuse(*, create: bool = True) -> Path:
        raise runtime.RuntimeDirError("refusing the runtime directory: other users can write")

    monkeypatch.setattr(runtime, "runtime_dir", refuse)
    reply = call_tool(write, dict(args))
    text = reply["content"][0]["text"]
    assert reply["isError"] and "edit lock is unavailable" in text, text
    assert "try again" not in text


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

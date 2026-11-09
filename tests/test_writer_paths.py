"""write_entry must never place an entry outside the memory tree."""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from smriti.core.dayzone import day_tz
from smriti.core.safe_names import UnsafeName
from smriti.store.writer import write_entry


@pytest.mark.parametrize("branch", [
    "../escape", "notes/../../escape", "..", "/etc/x", "C:/Windows/x", "C:\\x",
    "~/x", "", "/", "notes//x", "./notes",
])
def test_branches_that_leave_the_tree_are_rejected(tmp_path: Path, branch: str) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    with pytest.raises(ValueError, match="invalid branch"):
        write_entry("x", branch=branch, root=root, reindex=False)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("branch", [
    ".git", ".git/hooks", ".smriti/x", "notes/.hidden", "notes\\.git", "notes/x.",
    "notes/con", "projects/nul.txt", "notes/a*b", "notes/~x",
])
def test_each_segment_must_be_a_plain_name(tmp_path: Path, branch: str) -> None:
    """H11.5: the branch check agrees with safe_name, segment by segment. It
    let `.git` and `.smriti/x` through, so an agent could write into git's
    own directory or smriti's runtime state."""
    root = tmp_path / "tree"
    root.mkdir()
    with pytest.raises(UnsafeName, match="invalid branch"):
        write_entry("x", branch=branch, root=root, reindex=False)
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_an_unsafe_branch_is_a_plain_refusal_through_mcp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.mcp.server import handle_message

    root = tmp_path / "tree"
    root.mkdir()
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "smriti_write", "arguments": {"content": "x", "branch": ".git/hooks"}}})
    assert reply is not None
    result = reply["result"]
    assert result.get("isError") is True
    assert "invalid branch '.git/hooks'" in result["content"][0]["text"]
    assert not (root / ".git").exists()


@pytest.mark.parametrize("branch,expect", [
    ("notes", "notes"), ("projects/smriti", "projects/smriti"),
    ("projects/smriti/", "projects/smriti"), ("projects\\smriti", "projects/smriti"),
    ("notes/v0.2", "notes/v0.2"), ("projects/My Project", "projects/My Project"),
])
def test_normal_branches_still_write_inside_the_tree(tmp_path: Path, branch: str,
                                                     expect: str) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    path = write_entry("hello", branch=branch, root=root, reindex=False)
    assert path.resolve().is_relative_to(root.resolve())
    assert path.relative_to(root).as_posix().startswith(expect + "/")
    assert f"branch: {expect}" in path.read_text(encoding="utf-8")


def test_branch_through_a_link_out_of_the_tree_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    outside = tmp_path / "other-repo"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "mirrors", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    with pytest.raises(ValueError, match="outside the memory tree"):
        write_entry("x", branch="mirrors/notes", root=root, reindex=False)
    assert not any(outside.iterdir())


def _link_dir(link: Path, target: Path) -> None:
    """A directory link at ``link``: a symlink, or on Windows without the
    privilege a junction (which needs none); skip where neither can be made."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("symlinks not permitted here")
    import _winapi

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        pytest.skip("neither a symlink nor a junction can be made here")


def _today(root: Path) -> datetime:
    return datetime.now(timezone.utc).astimezone(day_tz(root))


@pytest.mark.parametrize("branch,dated", [
    ("notes", lambda now: f"notes/{now:%Y}"),
    ("projects/smriti", lambda now: f"projects/smriti/{now:%Y}"),
    ("journal", lambda now: f"journal/{now:%Y}"),
    ("journal", lambda now: f"journal/{now:%Y}/{now:%m}"),
], ids=["notes-year", "project-year", "journal-year", "journal-month"])
def test_a_dated_directory_linked_out_of_the_tree_is_refused(
        tmp_path: Path, branch: str, dated: Callable[[datetime], str]) -> None:
    """H15.5: the branch was checked, but not the year (or month) directory the
    writer adds to it: a link there sent the entry, and the directories made
    for it, outside the tree."""
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / dated(_today(root))
    link.parent.mkdir(parents=True)
    _link_dir(link, outside)

    with pytest.raises(UnsafeName, match="resolves outside the memory tree"):
        write_entry("x", branch=branch, root=root, reindex=False)
    assert list(outside.iterdir()) == []


def test_a_day_file_linked_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    """H15.5: the day's file itself, a symlink to a file elsewhere, was appended to."""
    root = tmp_path / "tree"
    outside = tmp_path / "outside.md"
    outside.write_text("someone else's file\n", encoding="utf-8")
    day = root / "notes" / f"{_today(root):%Y}" / f"{_today(root):%m-%d}.md"
    day.parent.mkdir(parents=True)
    try:
        os.symlink(outside, day)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks not permitted here")

    with pytest.raises(UnsafeName, match="resolves outside the memory tree"):
        write_entry("x", branch="notes", root=root, reindex=False)
    assert outside.read_text(encoding="utf-8") == "someone else's file\n"


def test_a_link_that_stays_inside_the_tree_is_followed(tmp_path: Path) -> None:
    """A link is refused only when it leads out: one to elsewhere in the tree still writes."""
    root = tmp_path / "tree"
    archive = root / "archive"
    archive.mkdir(parents=True)
    year = root / "notes" / f"{_today(root):%Y}"
    year.parent.mkdir()
    _link_dir(year, archive)

    path = write_entry("kept", branch="notes", root=root, reindex=False)

    [entry] = archive.iterdir()
    assert entry.name == path.name and "kept" in entry.read_text(encoding="utf-8")


def test_writers_racing_to_make_a_day_are_not_refused(tmp_path: Path) -> None:
    """The final-path check resolved the day's directory before making it.
    On Windows, resolving a path that another writer creates meanwhile can
    come back in its extended-length form, which isn't inside the tree, so
    the first writes of a day, made together, were refused (2 rounds in 3,
    eight writers at a time). Only paths that exist are resolved now."""
    for round_ in range(8):  # each round failed 2 times in 3 before
        root = tmp_path / f"tree{round_}"
        root.mkdir()
        start = threading.Barrier(8)
        errors: list[BaseException] = []

        def write(i: int, root: Path = root, start: threading.Barrier = start,
                  errors: list[BaseException] = errors) -> None:
            start.wait()
            try:
                write_entry(f"entry {i}", branch="notes", root=root, reindex=False)
            except Exception as exc:  # noqa: BLE001 — collected, asserted on the main thread
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, (round_, errors)
        [day] = (root / "notes").rglob("*.md")
        assert all(f"entry {i}" in day.read_text(encoding="utf-8") for i in range(8))

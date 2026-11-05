"""write_entry must never place an entry outside the memory tree."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

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

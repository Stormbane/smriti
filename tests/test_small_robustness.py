"""Small robustness fixes from outside review #3 (H9.7)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from smriti.core.tree import tree_root


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_private_key_is_owner_only(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from smriti.private.store import PrivateStore

    PrivateStore(tmp_path).init()
    mode = stat.S_IMODE(os.stat(tmp_path / "private" / ".key").st_mode)
    assert mode == 0o600


def test_the_private_key_is_written_once(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from smriti.private.store import PrivateStore

    PrivateStore(tmp_path).init()
    key = (tmp_path / "private" / ".key").read_text(encoding="ascii")
    PrivateStore(tmp_path).init()
    assert (tmp_path / "private" / ".key").read_text(encoding="ascii") == key


def _todo(lines: str) -> Path:
    todo = tree_root() / "mirrors" / "demo" / "ai" / "todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text(f"# TODO\n\n## Active\n{lines}", encoding="utf-8")
    return todo


def test_a_todo_topic_is_matched_as_a_whole_phrase() -> None:
    from smriti.mcp.tools.projects import handle_add_todo

    todo = _todo("- [ ] prefix the release notes\n")
    reply = handle_add_todo({"project": "demo", "topic": "fix", "line": "- [ ] fix the build",
                             "kind": "fix"})
    assert reply.startswith("Added") and "fix the build" in todo.read_text(encoding="utf-8")


def test_a_repeated_topic_is_still_skipped() -> None:
    from smriti.mcp.tools.projects import handle_add_todo

    _todo("- [ ] Fix   the BUILD on Linux\n")
    reply = handle_add_todo({"project": "demo", "topic": "fix the build",
                             "line": "- [ ] fix the build", "kind": "fix"})
    assert reply.startswith("Skipped")


def test_add_todo_without_any_mirrors_says_so() -> None:
    from smriti.mcp.tool import ToolError
    from smriti.mcp.tools.projects import handle_add_todo

    with pytest.raises(ToolError, match="no .ai/todo.md mirror.*\\(none\\)"):
        handle_add_todo({"project": "demo", "topic": "t", "line": "l", "kind": "fix"})


def test_the_mcp_read_handle_follows_a_root_change(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.mcp.tools import memory
    from smriti.store.indexer import index_tree

    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        (root / "notes").mkdir(parents=True)
        (root / "notes" / f"{name}.md").write_text(f"# {name}\n\nonly in root {name}\n",
                                                   encoding="utf-8")
        index_tree(root=root, db=root / ".smriti" / "index.db")
        roots.append(root)
    monkeypatch.setattr(memory, "_db", None)
    for root, name in zip(roots, ("a", "b"), strict=True):
        monkeypatch.setenv("SMRITI_ROOT", str(root))
        assert f"notes/{name}.md" in memory.handle_read({"query": f"only in root {name}"})
    assert memory._db is not None
    memory._db.close()
    monkeypatch.setattr(memory, "_db", None)

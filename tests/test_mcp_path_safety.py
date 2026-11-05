"""No MCP tool can be steered into reading or writing outside the memory tree."""
from __future__ import annotations

from pathlib import Path

import pytest

from smriti.core.safe_names import UnsafeName, safe_name
from smriti.mcp.server import handle_message

TRAVERSAL = ["../escape", "..", "a/b", "a\\b", "/etc", "C:x", "~x", ".hidden", "x\ny",
             "..\\..\\escape", "proj/../../escape", ".", "CON", "nul.txt", "trailing.", "",
             "COM¹", "lpt³.md", "CON .txt", "conin$"]


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tree"
    (root / "mirrors" / "demo" / "ai" / "features" / "active").mkdir(parents=True)
    (root / "mirrors" / "demo" / "ai" / "todo.md").write_text("# TODO\n", encoding="utf-8")
    (root / "outbox").mkdir()
    (root / "outbox" / "comms.md").write_text("## Q1\n\n**Status:** open\n\n---\n",
                                              encoding="utf-8")
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    return root


def _call(name: str, arguments: dict) -> dict:
    resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    assert resp is not None
    return resp["result"]


def _outside_files(tmp_path: Path) -> list[Path]:
    return [p for p in tmp_path.rglob("*") if p.is_file() and "tree" not in p.parts]


@pytest.mark.parametrize("bad", TRAVERSAL)
def test_safe_name_rejects_anything_path_like(bad: str) -> None:
    with pytest.raises(UnsafeName):
        safe_name(bad)


@pytest.mark.parametrize("good", ["demo", "beautiful-tree", "react_idle.timeout", "v2",
                                  "My Project", "projét", "a..b"])
def test_safe_name_accepts_plain_names(good: str) -> None:
    assert safe_name(f" {good} ") == good


@pytest.mark.parametrize("tool,args", [
    ("smriti_add_todo", {"task": "t"}),
    ("smriti_record_finding", {"title": "t", "body": "b"}),
    ("smriti_record_decision", {"title": "t", "decision": "d", "rationale": "r"}),
    ("smriti_record_feature", {"slug": "f", "title": "t", "goal": "g", "plan": "p"}),
    ("smriti_update_feature_status", {"slug": "f", "status": "shipped"}),
])
@pytest.mark.parametrize("bad", ["../escape", "..\\..\\escape", "/abs"])
def test_project_traversal_is_refused(tree: Path, tmp_path: Path, tool: str, args: dict,
                                      bad: str) -> None:
    result = _call(tool, {**args, "project": bad})
    assert result.get("isError") is True
    assert "single plain name" in result["content"][0]["text"]
    assert _outside_files(tmp_path) == []


@pytest.mark.parametrize("tool", ["smriti_record_feature", "smriti_update_feature_status"])
def test_slug_traversal_is_refused(tree: Path, tmp_path: Path, tool: str) -> None:
    args = {"project": "demo", "slug": "../../escape", "title": "t", "goal": "g", "plan": "p",
            "status": "shipped"}
    result = _call(tool, args)
    assert result.get("isError") is True
    assert _outside_files(tmp_path) == []


def test_outbox_file_traversal_is_refused(tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "victim.md"
    outside.write_text("## Q1\n\n**Status:** open\n\n---\n", encoding="utf-8")
    result = _call("smriti_resolve_outbox", {"file": "../../victim", "item": "Q1",
                                              "resolution": "answered"})
    assert result.get("isError") is True
    assert outside.read_text(encoding="utf-8").count("open") == 1


def test_plain_names_still_work(tree: Path) -> None:
    result = _call("smriti_add_todo", {"project": "demo", "topic": "docs",
                                       "line": "write the docs", "kind": "build"})
    assert not result.get("isError"), result
    assert "write the docs" in (tree / "mirrors" / "demo" / "ai" / "todo.md").read_text(
        encoding="utf-8")


def test_error_replies_are_marked_as_errors(tree: Path) -> None:
    result = _call("smriti_add_todo", {"project": "nonexistent", "topic": "t",
                                       "line": "x", "kind": "build"})
    assert result.get("isError") is True

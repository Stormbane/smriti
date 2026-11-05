"""MCP tool arguments are held to each tool's input schema (H11.5).

A handler calls ``.strip()`` on what it expects to be a string, and passed
``top_k`` straight to the search, so a number where a string belongs, or a
``top_k`` of a million, failed deep inside a handler (or didn't fail at all)
instead of being refused with a reason the agent can act on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from smriti.mcp.server import handle_message


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tree"
    (root / "mirrors" / "demo" / "ai").mkdir(parents=True)
    (root / "mirrors" / "demo" / "ai" / "todo.md").write_text("# TODO\n", encoding="utf-8")
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    return root  # no index: an argument that passes the checks meets "no index yet"


def _call(name: str, arguments: Any) -> tuple[bool, str]:
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
    assert reply is not None
    result = reply["result"]
    return bool(result.get("isError")), str(result["content"][0]["text"])


@pytest.mark.parametrize("tool,arguments,message", [
    ("smriti_read", {"query": 5}, "query must be a string, not a number"),
    ("smriti_read", {"query": ["brass", "lantern"]}, "query must be a string, not an array"),
    ("smriti_read", {"query": True}, "query must be a string, not a boolean"),
    ("smriti_write", {"content": {"text": "x"}}, "content must be a string, not an object"),
    ("smriti_write", {"content": "x", "branch": 3}, "branch must be a string, not a number"),
    ("smriti_write", {"content": "x", "title": 7}, "title must be a string, not a number"),
    ("smriti_write_handoff", {"content": 1}, "content must be a string, not a number"),
    ("smriti_add_todo", {"project": 1, "topic": "t", "line": "- [ ] t", "kind": "fix"},
     "project must be a string, not a number"),
    ("smriti_record_decision", {"project": "demo", "title": "t", "decision": ["d"],
                                "rationale": "r"}, "decision must be a string, not an array"),
])
def test_a_non_string_is_refused_by_name(tree: Path, caplog: pytest.LogCaptureFixture,
                                         tool: str, arguments: dict[str, Any],
                                         message: str) -> None:
    with caplog.at_level(logging.ERROR, logger="smriti.mcp.server"):
        failed, text = _call(tool, arguments)
    assert failed and text == f"Error: {message}"
    assert not caplog.records  # a refusal, not a handler that blew up and was logged
    assert [p for p in tree.rglob("*") if p.is_file() and p.name != "todo.md"] == []


@pytest.mark.parametrize("top_k,message", [
    (0, "top_k must be from 1 to 50 (got 0)"),
    (-3, "top_k must be from 1 to 50 (got -3)"),
    (51, "top_k must be from 1 to 50 (got 51)"),
    (10**6, "top_k must be from 1 to 50 (got 1000000)"),
    ("5", "top_k must be an integer, not a string"),
    (True, "top_k must be an integer, not a boolean"),
    (2.5, "top_k must be an integer, not a number"),
    ([5], "top_k must be an integer, not an array"),
])
def test_top_k_is_an_integer_in_bounds(tree: Path, top_k: object, message: str) -> None:
    failed, text = _call("smriti_read", {"query": "brass lantern", "top_k": top_k})
    assert failed and text == f"Error: {message}"


@pytest.mark.parametrize("top_k", [1, 5, 50, 5.0, None])
def test_a_good_top_k_reaches_the_handler(tree: Path, top_k: object) -> None:
    failed, text = _call("smriti_read", {"query": "brass lantern", "top_k": top_k})
    assert failed and "no index yet" in text  # past the checks, into the handler


def test_a_null_is_taken_as_not_given(tree: Path) -> None:
    failed, text = _call("smriti_write_handoff", {"content": "Pick up at step 3.",
                                                  "summary": None})
    assert not failed, text
    handoff = (tree / ".smriti" / "handoff-pending.md").read_text(encoding="utf-8")
    assert "Pick up at step 3." in handoff and "summary:" not in handoff


def test_the_bounds_are_in_the_listing() -> None:
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    read = next(t for t in reply["result"]["tools"] if t["name"] == "smriti_read")
    top_k = read["inputSchema"]["properties"]["top_k"]
    assert (top_k["minimum"], top_k["maximum"]) == (1, 50)


def test_arguments_that_are_not_an_object_are_still_refused(tree: Path) -> None:
    assert _call("smriti_read", ["brass lantern"]) == (True, "Error: arguments must be an object")

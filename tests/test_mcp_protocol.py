"""The MCP protocol and its one error model: refusals and failures are isError."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from smriti.mcp.server import call_tool, handle_message
from smriti.mcp.tool import Tool, ToolError
from smriti.mcp.tools import TOOLS

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "smriti" / "mcp" / "tools"


def _call(name: str, arguments: Any) -> dict[str, Any]:
    response = handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
    assert response is not None
    return response


def _text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def test_a_refusal_is_an_error_result() -> None:
    result = _call("smriti_add_todo", {"project": "demo"})["result"]
    assert result["isError"] is True
    assert _text(result).startswith("Error: ") and "required" in _text(result)


def test_an_unexpected_failure_is_an_error_result() -> None:
    def broken(arguments: dict[str, Any]) -> str:
        raise KeyError("lost")

    result = call_tool(Tool("broken", "", {"type": "object"}, broken), {})
    assert result["isError"] is True
    assert "lost" in _text(result)


def test_success_carries_no_error_flag() -> None:
    def fine(arguments: dict[str, Any]) -> str:
        return "done"

    assert call_tool(Tool("fine", "", {"type": "object"}, fine), {}) == {
        "content": [{"type": "text", "text": "done"}]}


@pytest.mark.parametrize("arguments", [["not", "an", "object"], "text", 3])
def test_arguments_must_be_an_object(arguments: Any) -> None:
    result = _call("smriti_status", arguments)["result"]
    assert result["isError"] is True and "object" in _text(result)


def test_a_path_argument_that_escapes_is_refused() -> None:
    result = _call("smriti_add_todo", {"project": "../x", "topic": "t", "line": "l",
                                       "kind": "fix"})["result"]
    assert result["isError"] is True and "single plain name" in _text(result)


def test_unknown_tool_and_method_are_protocol_errors() -> None:
    assert _call("no_such_tool", {})["error"]["code"] == -32601
    response = handle_message({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
    assert response is not None and response["error"]["code"] == -32601


def test_handshake_ping_and_notifications() -> None:
    init = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init is not None and init["result"]["serverInfo"]["name"] == "smriti"
    assert handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"}) == {
        "jsonrpc": "2.0", "id": 2, "result": {}}
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_shows_every_tool_once() -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    listed = response["result"]["tools"]
    assert [t["name"] for t in listed] == [t.name for t in TOOLS]
    assert len({t.name for t in TOOLS}) == len(TOOLS)
    for entry in listed:
        assert set(entry) == {"name", "description", "inputSchema"}
        assert entry["inputSchema"]["type"] == "object"


def _returns_error_text(node: ast.Return) -> bool:
    value = node.value
    if isinstance(value, ast.JoinedStr) and value.values:
        value = value.values[0]
    return (isinstance(value, ast.Constant) and isinstance(value.value, str)
            and value.value.startswith("Error"))


@pytest.mark.parametrize("module", sorted(p.name for p in TOOLS_DIR.glob("*.py")))
def test_no_handler_reports_failure_as_text(module: str) -> None:
    """Refusals raise ToolError; an "Error:" string would read as success to a caller."""
    tree = ast.parse((TOOLS_DIR / module).read_text(encoding="utf-8"))
    bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Return) and _returns_error_text(n)]
    assert not bad, f"{module} returns error text at lines {bad}; raise ToolError instead"


def test_tool_error_is_the_refusal_type() -> None:
    assert issubclass(ToolError, Exception) and not issubclass(ToolError, OSError)

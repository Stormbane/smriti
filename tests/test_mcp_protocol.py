"""The MCP protocol and its one error model: refusals and failures are isError."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from smriti.mcp.server import answer_line, call_tool, handle_message
from smriti.mcp.tool import Tool, ToolError
from smriti.mcp.tools import TOOLS

SRC = Path(__file__).resolve().parents[1] / "src" / "smriti"
TOOL_MODULES = sorted([*(SRC / "mcp" / "tools").glob("*.py"),
                       *(SRC / "profiles").rglob("*.py")])


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


@pytest.mark.parametrize("params", [{"name": ["a"]}, {"name": {"x": 1}}, {"name": 3}, ["x"], "x"])
def test_malformed_tool_calls_are_protocol_errors(params: Any) -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": params})
    assert response is not None and response["error"]["code"] in (-32601, -32602)


def test_the_server_survives_malformed_messages() -> None:
    """Over stdio, a bad message gets an error and the next one is still answered."""
    import json
    import subprocess
    import sys

    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["a"]}},
        {"jsonrpc": "2.0", "id": 2, "method": ["not", "a", "string"]},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    proc = subprocess.run([sys.executable, "-m", "smriti.mcp_server"],
                          input="".join(json.dumps(m) + "\n" for m in lines),
                          capture_output=True, text=True, timeout=60, check=True)
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert "error" in replies[0] and "error" in replies[1]
    assert replies[2]["result"] == {}


def _null_id_error(response: dict[str, Any] | None) -> int:
    """The code of ``response``, an error with a null id (the one shape it may have)."""
    assert response is not None and set(response) == {"jsonrpc", "id", "error"}
    assert response["jsonrpc"] == "2.0" and response["id"] is None
    assert isinstance(response["error"]["message"], str) and response["error"]["message"]
    return int(response["error"]["code"])


@pytest.mark.parametrize("line", [
    "not json",
    '{"jsonrpc": "2.0", "id": 1, "method": "ping"',
    "{'jsonrpc': '2.0'}",
    # Deeper than the parser recurses: a RecursionError, which used to end the loop.
    "[" * 100_000 + "]" * 100_000,
    # A byte that wasn't UTF-8, as main() reads it (surrogateescape).
    '{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "\udcff"}}',
], ids=["text", "cut short", "single quotes", "nested too deep", "not utf-8"])
def test_a_line_that_is_not_json_gets_a_parse_error(line: str) -> None:
    """H12.16: JSON-RPC 2.0 answers it with -32700 and a null id; it went unanswered."""
    assert _null_id_error(answer_line(line)) == -32700


@pytest.mark.parametrize("line", ["[]", '[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]',
                                  '"ping"', "3", "null", "true"])
def test_json_that_is_not_an_object_is_an_invalid_request(line: str) -> None:
    assert _null_id_error(answer_line(line)) == -32600


def test_a_well_formed_line_is_answered_as_before() -> None:
    line = '{"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {"note": "café स्मृति"}}'
    assert answer_line(line) == {"jsonrpc": "2.0", "id": 5, "result": {}}
    assert answer_line('{"jsonrpc": "2.0", "method": "notifications/initialized"}') is None


def test_the_server_answers_bad_lines_and_goes_on() -> None:
    """Over stdio: a line that isn't JSON, isn't UTF-8 (which ended the server), or
    isn't an object each gets its error, and the messages after them are answered."""
    import json
    import subprocess
    import sys

    stdin = b"".join([
        b"not json\n",
        b"\xff\xfe{}\n",
        b"[1, 2]\n",
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping",
                    "params": {"note": "café"}}, ensure_ascii=False).encode("utf-8") + b"\n",
        b'{"jsonrpc": "2.0", "id": 8, "method": "ping"}\n',
    ])
    proc = subprocess.run([sys.executable, "-m", "smriti.mcp_server"], input=stdin,
                          capture_output=True, timeout=60, check=True)
    replies = [json.loads(line) for line in proc.stdout.decode("utf-8").splitlines()]
    assert [(r["id"], r.get("error", {}).get("code")) for r in replies] == [
        (None, -32700), (None, -32700), (None, -32600), (7, None), (8, None)]
    assert replies[3]["result"] == {} and replies[4]["result"] == {}


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


@pytest.mark.parametrize("module", [p.relative_to(SRC).as_posix() for p in TOOL_MODULES])
def test_no_handler_reports_failure_as_text(module: str) -> None:
    """Refusals raise ToolError; an "Error:" string would read as success to a caller."""
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Return) and _returns_error_text(n)]
    assert not bad, f"{module} returns error text at lines {bad}; raise ToolError instead"


def test_tool_error_is_the_refusal_type() -> None:
    assert issubclass(ToolError, Exception) and not issubclass(ToolError, OSError)

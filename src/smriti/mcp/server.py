"""The MCP protocol: JSON-RPC 2.0 over stdio, one message per line.

``handle_message`` answers one request; ``main`` reads stdin until it
closes. Tools are looked up by name in a table built from
:data:`smriti.mcp.tools.TOOLS`.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from smriti.core.safe_names import PATH_ARGUMENTS, UnsafeName, safe_name
from smriti.mcp.tool import Tool, ToolError
from smriti.mcp.tools import TOOLS

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "smriti", "version": "0.1.0"}
CAPABILITIES: dict[str, Any] = {"tools": {}}

_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}

Message = dict[str, Any]


def _response(id: Any, result: dict[str, Any]) -> Message:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _error(id: Any, code: int, message: str) -> Message:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _text(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def call_tool(tool: Tool, arguments: object) -> dict[str, Any]:
    """Run ``tool``. Any failure comes back as an ``isError`` result the agent can read."""
    try:
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        # Any argument that becomes a path segment must be a plain name,
        # whichever tool receives it (see smriti.core.safe_names).
        for key in PATH_ARGUMENTS:
            if arguments.get(key) not in (None, ""):
                arguments[key] = safe_name(arguments[key], key)
        return _text(tool.handler(arguments))
    except (ToolError, UnsafeName) as exc:
        return _text(f"Error: {exc}", is_error=True)
    except Exception as exc:
        log.exception("tool %s failed", tool.name)
        return _text(f"Error: {exc}", is_error=True)


def handle_message(msg: Message) -> Message | None:
    """The response to one JSON-RPC message, or None for a notification."""
    method = msg.get("method", "")
    id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _response(id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })
    if method == "tools/list":
        return _response(id, {"tools": [tool.listing() for tool in TOOLS]})
    if method == "tools/call":
        tool = _BY_NAME.get(params.get("name", ""))
        if tool is None:
            return _error(id, -32601, f"Unknown tool: {params.get('name', '')}")
        return _response(id, call_tool(tool, params.get("arguments", {})))
    if method == "ping":
        return _response(id, {})
    if method.startswith("notifications/"):
        return None
    if id is not None:
        return _error(id, -32601, f"Method not found: {method}")
    return None


def main() -> None:
    """Run the MCP server over stdio."""
    # UTF-8 for Windows
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

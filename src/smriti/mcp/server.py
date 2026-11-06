"""The MCP protocol: JSON-RPC 2.0 over stdio, one message per line.

``handle_message`` answers one request; ``main`` reads stdin until it
closes. Tools are looked up by name among
:func:`smriti.mcp.tools.active_tools`: the core's, plus those of the
profile the memory root's config names. The table is read per request,
so it follows the root (``SMRITI_ROOT``) and its config.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from smriti.core.safe_names import PATH_ARGUMENTS, UnsafeName, safe_name
from smriti.mcp.tool import Tool, ToolError, check_arguments
from smriti.mcp.tools import active_tools

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "smriti", "version": "0.1.0"}
CAPABILITIES: dict[str, Any] = {"tools": {}}

Message = dict[str, Any]

# Set by main(): this process is a session's server. Its first tool call then
# marks it as holding the embedding service (smriti.embed.hold_while_running),
# so the model stays loaded until the session ends; at that call, not at
# startup, so a server starts with exactly the imports it had before.
_hold_at_first_call = False


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
        # Every tool's arguments hold to its input schema's types and bounds,
        # so a handler never meets a number where it expects a string.
        check_arguments(tool.input_schema, arguments)
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
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if not isinstance(method, str):
        return _error(id, -32600, "Invalid request: method must be a string")

    if method == "initialize":
        return _response(id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })
    if method == "tools/list":
        return _response(id, {"tools": [tool.listing() for tool in active_tools()]})
    if method == "tools/call":
        if _hold_at_first_call:
            _hold_service()
        name = params.get("name")
        if not isinstance(name, str):
            return _error(id, -32602, "Invalid params: name must be a string")
        tool = next((t for t in active_tools() if t.name == name), None)
        if tool is None:
            return _error(id, -32601, f"Unknown tool: {name}")
        return _response(id, call_tool(tool, params.get("arguments", {})))
    if method == "ping":
        return _response(id, {})
    if method.startswith("notifications/"):
        return None
    if id is not None:
        return _error(id, -32601, f"Method not found: {method}")
    return None


def _hold_service() -> None:
    """Hold the embedding service from this process's first embedding on (once)."""
    global _hold_at_first_call
    _hold_at_first_call = False
    from smriti.embed import hold_while_running

    hold_while_running()


def main() -> None:
    """Run the MCP server over stdio."""
    global _hold_at_first_call
    # A session's server lives as long as the session: once it has embedded,
    # it keeps the embedding service loaded until it exits (smriti.embed.client).
    _hold_at_first_call = True
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
        try:
            response = handle_message(msg)
        except Exception:
            # One malformed message must never end the server for the session.
            log.exception("could not handle %.200r", msg)
            id = msg.get("id")
            response = None if id is None else _error(id, -32603, "Internal error")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

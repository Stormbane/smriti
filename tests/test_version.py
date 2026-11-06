"""H12.5: the MCP server reports the package's version, and tool texts match their code.

Outside review #5: ``serverInfo.version`` said 0.1.0 against package 0.2.0,
and ``smriti_add_todo`` said it "substring-dedups" though it matches whole
phrases.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import smriti
from smriti.mcp.server import handle_message
from smriti.mcp.tools import TOOLS

REPO = Path(__file__).resolve().parents[1]


def test_the_server_reports_the_package_version() -> None:
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert reply is not None
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert reply["result"]["serverInfo"] == {"name": "smriti", "version": smriti.__version__}
    assert smriti.__version__ == project["version"]


def test_add_todo_says_how_it_dedupes() -> None:
    from smriti.mcp.tools.projects import _mentions

    tool = next(t for t in TOOLS if t.name == "smriti_add_todo")
    assert "whole phrase" in tool.description
    assert not re.search(r"substring", tool.description, re.IGNORECASE)
    assert _mentions("- [ ] Fix the lamp wick", "fix the  LAMP")
    assert not _mentions("- [ ] Add a prefix to names", "fix")

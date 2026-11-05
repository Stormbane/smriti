"""Every tool the server offers, in the order ``tools/list`` shows them."""

from __future__ import annotations

from smriti.mcp.tool import Tool
from smriti.mcp.tools import memory, outbox, projects

TOOLS: tuple[Tool, ...] = (*memory.TOOLS, *projects.TOOLS, *outbox.TOOLS)

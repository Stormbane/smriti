"""smriti's MCP server: memory tools for an agent, over stdio, with no dependencies.

- :mod:`smriti.mcp.server` — the JSON-RPC protocol and the tool table;
- :mod:`smriti.mcp.tool` — what a tool is, and :class:`~smriti.mcp.tool.ToolError`;
- :mod:`smriti.mcp.tools` — the core tools, one module per group, and
  :func:`~smriti.mcp.tools.active_tools`, which adds the configured
  profile's (:mod:`smriti.profiles`).

Run it with ``python -m smriti.mcp`` (or ``python -m smriti.mcp_server``,
the name installed configs use). Pin the interpreter's absolute path in the
agent's MCP config: a bare ``python`` resolves against the spawning
process's PATH, which may be a venv without smriti::

    {"mcpServers": {"smriti": {"command": "C:/path/to/python.exe",
                               "args": ["-m", "smriti.mcp_server"]}}}
"""

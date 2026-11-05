"""Entry point kept for installed MCP configs: ``python -m smriti.mcp_server``.

The server lives in :mod:`smriti.mcp`.
"""

from smriti.mcp.server import handle_message, main

__all__ = ["handle_message", "main"]

if __name__ == "__main__":
    main()

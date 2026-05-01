"""Harness-neutral building blocks for integration adapters.

Functionality shared between every harness adapter
(Claude Code, Codex, future Cursor / Cline / Continue.dev) lives here
so a fix or change happens in one place. Per-harness modules import
from this package and only carry the harness-specific glue (config
file format, hook command shape, harness-specific addendum text).

Public surface:

    SMRITI_MCP_COMMAND      — the canonical MCP server invocation spec
    compose_agent_doc       — AGENT.md → harness CLAUDE.md/AGENTS.md
    deploy_hook_scripts     — idempotent script copy with checksum skip
    wake_runner.run         — emit the wake briefing in the requested
                              framing (raw stdout vs codex-json)

The adapter pattern is:
    1. Import shared helpers from this package.
    2. Read/parse the harness's config file (JSON for Claude, TOML for
       Codex) using the harness's native format.
    3. Mutate the parsed dict, leaning on shared constants for the
       smriti-side values.
    4. Write back with a backup, idempotently.
"""

from __future__ import annotations

from smriti.integrations.common.agent_md import compose_agent_doc
from smriti.integrations.common.hook_scripts import deploy_hook_scripts
from smriti.integrations.common.mcp_spec import (
    SMRITI_MCP_COMMAND,
    make_wake_hook_command,
)

__all__ = [
    "SMRITI_MCP_COMMAND",
    "compose_agent_doc",
    "deploy_hook_scripts",
    "make_wake_hook_command",
]

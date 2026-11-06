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

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from smriti.integrations.common.agent_md import compose_agent_doc
    from smriti.integrations.common.hook_model import (
        DEFICIENT,
        EQUIVALENT,
        UNRELATED,
        classify_wake_hook,
        make_wake_hook_command,
        parse_wake_command,
    )
    from smriti.integrations.common.hook_scripts import (
        check_deployed_hooks,
        deploy_hook_scripts,
    )
    from smriti.integrations.common.managed_doc import (
        ManagedDocError,
        classify_doc,
        migrate_agent_doc,
        write_managed_doc,
    )
    from smriti.integrations.common.mcp_spec import (
        SMRITI_MCP_COMMAND,
        mcp_registration_matches,
    )

# Shared deployable hook shims (one source, harness-local filenames).
SHARED_HOOKS_DIR = Path(__file__).resolve().parent / "hooks"

# Each helper's module, imported when the helper is first asked for: the
# wake hook runs ``wake_runner``, inside this package, in every session, and
# needs none of the installers' helpers.
_LAZY = {
    "compose_agent_doc": "agent_md",
    "DEFICIENT": "hook_model", "EQUIVALENT": "hook_model", "UNRELATED": "hook_model",
    "classify_wake_hook": "hook_model", "make_wake_hook_command": "hook_model",
    "parse_wake_command": "hook_model",
    "check_deployed_hooks": "hook_scripts", "deploy_hook_scripts": "hook_scripts",
    "ManagedDocError": "managed_doc", "classify_doc": "managed_doc",
    "migrate_agent_doc": "managed_doc", "write_managed_doc": "managed_doc",
    "SMRITI_MCP_COMMAND": "mcp_spec", "mcp_registration_matches": "mcp_spec",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # found directly from now on
    return value

__all__ = [
    "SMRITI_MCP_COMMAND",
    "SHARED_HOOKS_DIR",
    "EQUIVALENT",
    "DEFICIENT",
    "UNRELATED",
    "ManagedDocError",
    "check_deployed_hooks",
    "classify_doc",
    "classify_wake_hook",
    "compose_agent_doc",
    "deploy_hook_scripts",
    "make_wake_hook_command",
    "mcp_registration_matches",
    "migrate_agent_doc",
    "parse_wake_command",
    "write_managed_doc",
]

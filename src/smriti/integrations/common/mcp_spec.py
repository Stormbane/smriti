"""Canonical smriti MCP server spec, shared by every harness adapter.

All harnesses register the same Python entrypoint (``python -m
smriti.mcp_server``); only the *config-file format* differs (JSON for
Claude Code's ``~/.claude.json``, TOML for Codex's
``~/.codex/config.toml``). Centralising the spec means an MCP rename
or argv change happens in one place.

Wake-hook command generation lives in ``hook_model`` (alongside the
classifier, so generator and checker can never drift).
"""

from __future__ import annotations

# What every harness writes into its MCP server registry.
# Adapters convert this dict to their config-file's native format.
SMRITI_MCP_COMMAND: dict[str, object] = {
    "command": "python",
    "args": ["-m", "smriti.mcp_server"],
}


def mcp_registration_matches(actual: object) -> bool:
    """True when a registry entry carries the canonical command + args.

    Superset match, not equality: harnesses annotate their registry
    entries (Codex adds ``tools.*`` approval-mode subtables), and those
    extras must not read as "smriti is not registered".
    """
    if not isinstance(actual, dict):
        return False
    return all(actual.get(key) == value for key, value in SMRITI_MCP_COMMAND.items())


__all__ = ["SMRITI_MCP_COMMAND", "mcp_registration_matches"]

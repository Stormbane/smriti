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

import sys

# What every harness writes into its MCP server registry.
# Adapters convert this dict to their config-file's native format.
#
# The interpreter is pinned to the absolute path of the python running
# the installer — the one guaranteed to have smriti importable. A bare
# "python" resolves against the SPAWNING process's PATH, and sessions
# launched by other daemons (the hermes gateway leads PATH with its own
# venv Scripts/) resolved it to an interpreter without smriti: instant
# ModuleNotFoundError, surfaced to the harness as "Connection closed"
# (brain/chat sessions, 2026-09-06). An existing bare-"python"
# registration no longer matches, so installers repair it in place.
SMRITI_MCP_COMMAND: dict[str, object] = {
    "command": sys.executable,
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

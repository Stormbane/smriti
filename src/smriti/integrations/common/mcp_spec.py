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

import os
import sys
from pathlib import Path
from typing import TypedDict


class MCPCommand(TypedDict):
    command: str
    args: list[str]


class MCPRegistration(MCPCommand, total=False):
    env: dict[str, str]


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
SMRITI_MCP_COMMAND: MCPCommand = {
    "command": sys.executable,
    "args": ["-m", "smriti.mcp_server"],
}


# The server's memory root, in its registration: a harness starts the server
# with its own environment, where SMRITI_ROOT may be unset or another root's,
# and the server would then search and write the default root instead of the
# one the installer installed.
ROOT_VARIABLE = "SMRITI_ROOT"


def mcp_registration(memory_root: Path | None = None) -> MCPRegistration:
    """What a harness registers: :data:`SMRITI_MCP_COMMAND`, and the memory root.

    ``memory_root`` goes into the entry's ``env`` as ``SMRITI_ROOT``, absolute
    (``~`` expanded), so the server uses the root it was installed for
    whatever environment the harness starts it in.
    """
    entry: MCPRegistration = {"command": SMRITI_MCP_COMMAND["command"],
                              "args": list(SMRITI_MCP_COMMAND["args"])}
    if memory_root is not None:
        entry["env"] = {ROOT_VARIABLE: os.path.abspath(memory_root.expanduser())}
    return entry


def _root_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _same_root(value: object, memory_root: Path) -> bool:
    return isinstance(value, str) and bool(value) and _root_key(Path(value)) == _root_key(
        memory_root)


def mcp_registration_matches(actual: object, memory_root: Path | None = None) -> bool:
    """True when a registry entry carries the canonical command + args.

    Superset match, not equality: harnesses annotate their registry
    entries (Codex adds ``tools.*`` approval-mode subtables), and those
    extras must not read as "smriti is not registered". With
    ``memory_root``, the entry's ``env`` must also name that root, so an
    entry from before the root was registered is brought up to date.
    """
    if not isinstance(actual, dict):
        return False
    if not all(actual.get(key) == value for key, value in SMRITI_MCP_COMMAND.items()):
        return False
    if memory_root is None:
        return True
    env = actual.get("env")
    return isinstance(env, dict) and _same_root(env.get(ROOT_VARIABLE), memory_root)


__all__ = ["ROOT_VARIABLE", "SMRITI_MCP_COMMAND", "MCPRegistration", "mcp_registration",
           "mcp_registration_matches"]

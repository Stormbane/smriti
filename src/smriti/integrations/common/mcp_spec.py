"""Canonical smriti MCP server spec, shared by every harness adapter.

All harnesses register the same Python entrypoint (``python -m
smriti.mcp_server``); only the *config-file format* differs (JSON for
Claude Code's ``~/.claude.json``, TOML for Codex's
``~/.codex/config.toml``). Centralising the spec means an MCP rename
or argv change happens in one place.
"""

from __future__ import annotations

from pathlib import Path

# What every harness writes into its MCP server registry.
# Adapters convert this dict to their config-file's native format.
SMRITI_MCP_COMMAND: dict[str, object] = {
    "command": "python",
    "args": ["-m", "smriti.mcp_server"],
}


def make_wake_hook_command(
    memory_root: Path,
    *,
    home: Path,
    framing: str = "raw",
    audience: str = "coding",
) -> str:
    """Shell command string the harness runs at SessionStart.

    Both Claude Code and Codex shell out to the same ``wake.py`` in
    the entity tree (``$HOME/<memory_rel>/.smriti/wake.py``). The only
    knob is ``framing``: Claude Code wants raw stdout, Codex wants the
    JSON ``hookSpecificOutput.additionalContext`` shape.

    We use ``$HOME`` and forward slashes because both harnesses run
    hook commands under ``bash`` even on Windows, and bash mangles
    backslash-escaped paths.
    """
    if framing not in ("raw", "codex-json"):
        raise ValueError(f"unknown wake framing {framing!r}")
    memory_rel = memory_root.relative_to(home).as_posix()
    framing_var = (
        f'SMRITI_WAKE_FRAMING="{framing}" ' if framing != "raw" else ""
    )
    return (
        f'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/{memory_rel}" '
        f'SMRITI_WAKE_AUDIENCE="{audience}" {framing_var}'
        f'python "$HOME/{memory_rel}/.smriti/wake.py"'
    ).strip()


__all__ = ["SMRITI_MCP_COMMAND", "make_wake_hook_command"]

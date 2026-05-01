"""Codex CLI integration adapter.

Wires smriti into OpenAI's Codex CLI. Lives alongside Claude Code as
a peer adapter: same shared building blocks (``smriti.integrations
.common``), different config-file format and hook-command shape.

Public entry point:
    smriti.integrations.codex.install.run_codex(memory_root)
"""

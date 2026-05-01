"""Claude Code integration.

Wires smriti's core into Anthropic's Claude Code CLI:

    hooks/associative_recall.py   PostToolUse(Read|Edit|Write) shim
                                  → calls smriti.recall.hook.main
    hooks/precompact_capture.py   PreCompact event capture; writes
                                  conversation turns to a staging tree
                                  before context is compacted away.

Install glue currently lives in ``scripts/install.py`` (settings.json
patcher, CLAUDE.md template, MCP server registration in
``~/.claude.json``). Phase 4 of the agnosticism plan moves that glue
into this package as ``install.py`` so other harnesses can ship their
own equivalent.
"""

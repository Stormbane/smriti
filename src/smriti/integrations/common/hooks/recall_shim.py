"""Deployable PostToolUse recall shim — shared by every harness.

One source, deployed under each harness's local filename
(``~/.claude/hooks/associative_recall.py`` for Claude Code,
``~/.codex/hooks/recall_hook.py`` for Codex). Output framing is
selected by the environment the hook command sets
(``SMRITI_RECALL_FRAMING=codex-json`` for Codex; unset/raw for
Claude Code).

Delegates all logic to ``smriti.recall.hook`` so the deployed copy
stays a stable entry point even as the implementation evolves.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    try:
        from smriti.recall.hook import main
    except Exception:
        # smriti not importable -> silently no-op so we never break
        # the parent tool call. Run `pip install -e .` from the smriti
        # repo to enable recall.
        sys.exit(0)
    sys.exit(main())

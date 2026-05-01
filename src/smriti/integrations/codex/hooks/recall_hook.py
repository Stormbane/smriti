"""Deployable PostToolUse recall hook for Codex CLI.

Installed by ``smriti.integrations.codex.install`` to
``~/.codex/hooks/recall_hook.py`` and wired into ``~/.codex/config.toml``
under ``[[hooks.PostToolUse]]`` matching ``Edit|Write|apply_patch``.

Delegates all logic to ``smriti.recall.hook`` so the deployed copy
stays a stable entry point even as the implementation evolves. Output
framing is selected by the ``SMRITI_RECALL_FRAMING=codex-json``
environment variable that the hook command sets.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    try:
        from smriti.recall.hook import main
    except Exception:
        # smriti not importable -> silently no-op so we never break
        # the parent tool call.
        sys.exit(0)
    sys.exit(main())

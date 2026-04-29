"""Deployable associative-recall hook.

Installed by ``scripts/install.py`` to ``~/.claude/hooks/associative_recall.py``
and wired into ``~/.claude/settings.json`` as a PostToolUse hook on
``Read|Edit|Write``. Delegates all logic to ``smriti.recall.hook`` so the
deployed copy stays a stable entry point even as the implementation
evolves.
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

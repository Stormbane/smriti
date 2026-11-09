"""Harness-neutral SessionStart wake runner.

Both Claude Code's SessionStart hook and Codex's
``[[hooks.SessionStart]]`` hook run the same entry point from the
installed package, ``python -P -m smriti.wake`` (:mod:`smriti.wake.__main__`).
Output framing is the only thing that differs:

    raw          — print the briefing string verbatim. Claude Code
                   appends it to the session as plain context.
    codex-json   — emit ``{"hookSpecificOutput": {"hookEventName":
                   "SessionStart", "additionalContext": <briefing>}}``.
                   Codex requires this shape when the script wants to
                   inject context (it also accepts plain text, but the
                   JSON form is explicit and forward-compatible).

The hook selects framing via ``$SMRITI_WAKE_FRAMING`` — set to
``codex-json`` by the Codex install adapter, defaulting to ``raw``
for everyone else.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import cast

_ON = {"1", "full", "on", "true", "yes"}
_VALID_FRAMINGS = ("raw", "codex-json")


def _wake_enabled() -> bool:
    return os.environ.get("SMRITI_WAKE", "").strip().lower() in _ON


def _frame(payload: str, framing: str) -> str:
    if framing == "raw":
        return payload
    if framing == "codex-json":
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": payload,
            }
        })
    raise ValueError(f"unknown framing {framing!r}; expected one of {_VALID_FRAMINGS}")


def run(
    *,
    entity_root: Path | None = None,
    script_dir: Path | None = None,
    framing: str | None = None,
) -> int:
    """Emit the wake briefing, framed for the calling harness.

    ``entity_root`` defaults to :func:`smriti.core.tree.tree_root`
    (``$SMRITI_ROOT``, else an existing ``~/.narada``, else ``~/.smriti``).
    The ``last-activity`` touch-file is ``<entity_root>/.smriti/last-activity``;
    ``script_dir`` names another directory for it, as the ``wake.py`` an
    earlier install deployed into ``<root>/.smriti/`` passes its own (the
    same one) until a reinstall replaces its hook. Wake doesn't back the
    tree up: the hourly ``smriti-backup`` task does.
    ``framing`` defaults to ``$SMRITI_WAKE_FRAMING`` or ``raw``.
    """
    if not _wake_enabled():
        return 0

    if entity_root is None:
        from smriti.core.tree import tree_root

        entity_root = tree_root()
    if framing is None:
        framing = os.environ.get("SMRITI_WAKE_FRAMING", "raw").strip()

    # Heartbeat consumer reads this to know if the user is around.
    activity_dir = script_dir if script_dir is not None else entity_root / ".smriti"
    try:
        (activity_dir / "last-activity").touch()
    except OSError:
        pass

    try:
        cast(io.TextIOWrapper, sys.stdout).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    from smriti.wake import briefing
    from smriti.wake.briefing import consume_handoff, pending_handoff

    audience = os.environ.get("SMRITI_WAKE_AUDIENCE", "coding").strip() or "coding"
    # The Hands section reads the calling harness's MCP config.
    harness = "codex" if framing == "codex-json" else "claude"
    handoff = pending_handoff(entity_root)
    payload = briefing(
        memory_root=entity_root, cwd=Path(os.getcwd()), audience=audience, harness=harness,
    )
    sys.stdout.write(_frame(payload, framing))
    sys.stdout.flush()
    # The handoff letter is shown once: archive it only after the briefing
    # carrying it has been written out (and only if it wasn't rewritten).
    if handoff is not None and "--- HANDOFF from your last session" in payload:
        consume_handoff(entity_root, handoff[1])
    return 0


__all__ = ["run"]

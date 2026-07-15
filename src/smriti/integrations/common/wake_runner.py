"""Harness-neutral SessionStart wake runner.

Both Claude Code's SessionStart hook and Codex's
``[[hooks.SessionStart]]`` hook point at the same ``wake.py`` shim in
the entity tree (``~/.narada/.smriti/wake.py``). Output framing is
the only thing that differs:

    raw          — print the briefing string verbatim. Claude Code
                   appends it to the session as plain context.
    codex-json   — emit ``{"hookSpecificOutput": {"hookEventName":
                   "SessionStart", "additionalContext": <briefing>}}``.
                   Codex requires this shape when the script wants to
                   inject context (it also accepts plain text, but the
                   JSON form is explicit and forward-compatible).

The shim selects framing via ``$SMRITI_WAKE_FRAMING`` — set to
``codex-json`` by the Codex install adapter, defaulting to ``raw``
for everyone else.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ON = {"1", "full", "on", "true", "yes"}
_VALID_FRAMINGS = ("raw", "codex-json")


def _wake_enabled() -> bool:
    return os.environ.get("SMRITI_WAKE", "").strip().lower() in _ON


def _fire_backup(script_dir: Path) -> None:
    """Kick off commit+push in the background; never block wake."""
    script = script_dir / "backup.py"
    if not script.exists():
        return
    try:
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [sys.executable, str(script), "--tag", "wake", "--push"],
            **kwargs,
        )
    except Exception:
        pass


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

    ``entity_root`` defaults to ``$SMRITI_ROOT`` or ``~/.narada``.
    ``script_dir`` is where ``backup.py`` and the ``last-activity``
    touch-file live (typically the directory of the calling shim).
    ``framing`` defaults to ``$SMRITI_WAKE_FRAMING`` or ``raw``.
    """
    if not _wake_enabled():
        return 0

    if entity_root is None:
        entity_root = Path(
            os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada"))
        )
    if framing is None:
        framing = os.environ.get("SMRITI_WAKE_FRAMING", "raw").strip()

    if script_dir is not None:
        _fire_backup(script_dir)
        # Heartbeat consumer reads this to know if the user is around.
        try:
            (script_dir / "last-activity").touch()
        except OSError:
            pass

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        from smriti.wake import briefing
    except ImportError:
        # smriti not on path; fail silent so we never block the session.
        return 0

    audience = os.environ.get("SMRITI_WAKE_AUDIENCE", "coding").strip() or "coding"
    payload = briefing(memory_root=entity_root, cwd=Path(os.getcwd()), audience=audience)
    sys.stdout.write(_frame(payload, framing))
    return 0


__all__ = ["run"]

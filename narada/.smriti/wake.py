"""wake.py -- SessionStart loader for smriti-managed entities.

Thin wrapper around ``smriti.wake.briefing`` that handles the
harness-side concerns:

    SMRITI_WAKE=1|full|on|true    -> emit briefing to stdout
    SMRITI_WAKE=0|skip|off|unset  -> silent (e.g. ``claude -p``)

Plus: kicks off a background backup, touches last-activity for the
heartbeat consumer, and reconfigures stdout for utf-8 on Windows.

Briefing assembly itself lives in ``src/smriti/wake/briefing.py`` so
non-Claude-Code harnesses (Cursor, custom Python agents) can call it
directly without re-implementing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Entity root: set SMRITI_ROOT to use a custom path (e.g. ~/.tara).
ENTITY_ROOT = Path(os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada")))

_ON = {"1", "full", "on", "true", "yes"}


def _wake_enabled() -> bool:
    return os.environ.get("SMRITI_WAKE", "").strip().lower() in _ON


def _fire_backup() -> None:
    """Kick off commit+push in the background; never block wake."""
    script = Path(__file__).parent / "backup.py"
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


def main() -> int:
    if not _wake_enabled():
        return 0

    _fire_backup()

    # Heartbeat consumer reads this to know if the user is around.
    try:
        (Path(__file__).parent / "last-activity").touch()
    except OSError:
        pass

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    # Library call — pure assembly.
    try:
        from smriti.wake import briefing
    except ImportError:
        # smriti not on path; fail silent so we never block the session.
        return 0

    payload = briefing(memory_root=ENTITY_ROOT, cwd=Path(os.getcwd()))
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

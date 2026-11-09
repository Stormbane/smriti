"""wake.py — SessionStart shim for smriti-managed entities.

This file ships in the entity's ``.smriti/`` directory
(``<root>/.smriti/wake.py``) and is pointed at by every harness's
SessionStart hook. All real logic lives in
``smriti.integrations.common.wake_runner.run`` so a fix or behavior
change happens in one place across all harnesses.

Environment knobs:

    SMRITI_WAKE=1|full|on|true       emit briefing (default off so
                                     ``claude -p`` etc. stay clean)
    SMRITI_ROOT=/path/to/entity      override entity tree location
    SMRITI_WAKE_FRAMING=raw          plain stdout (Claude Code default)
                       =codex-json   {"hookSpecificOutput": ...} (Codex)

If smriti can't be imported by the interpreter running this (a hook that
runs another python than the one smriti is installed in), a briefing that
was asked for is missing: that is said in one line on stderr, naming the
interpreter, with exit status 1. Nothing goes to stdout, which the harness
adds to the session. Neither harness stops a session for a SessionStart
hook that fails: Claude Code shows a notice with stderr's first line (with
status 0 it discards stderr), and Codex reports a hook failure. With
SMRITI_WAKE off no briefing was asked for, and it exits 0 in silence.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ON = {"1", "full", "on", "true", "yes"}  # as wake_runner reads SMRITI_WAKE


def main() -> int:
    try:
        from smriti.integrations.common.wake_runner import run
    except ImportError as exc:
        if os.environ.get("SMRITI_WAKE", "").strip().lower() not in _ON:
            return 0
        sys.stderr.write(
            f"[wake] no memory briefing: {sys.executable or 'this python'} can't import "
            f"smriti ({exc}); install smriti for it, or re-run smriti's installer "
            "with the python that has it\n"
        )
        return 1
    return run(script_dir=Path(__file__).parent)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — never breaks SessionStart; reported on stderr
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

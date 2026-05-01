"""wake.py — SessionStart shim for smriti-managed entities.

This file ships in the entity's ``.smriti/`` directory (e.g.
``~/.narada/.smriti/wake.py``) and is pointed at by every harness's
SessionStart hook. All real logic lives in
``smriti.integrations.common.wake_runner.run`` so a fix or behavior
change happens in one place across all harnesses.

Environment knobs:

    SMRITI_WAKE=1|full|on|true       emit briefing (default off so
                                     ``claude -p`` etc. stay clean)
    SMRITI_ROOT=/path/to/entity      override entity tree location
    SMRITI_WAKE_FRAMING=raw          plain stdout (Claude Code default)
                       =codex-json   {"hookSpecificOutput": ...} (Codex)

If smriti is not importable we fail silent so no harness ever has its
SessionStart broken by a missing dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        from smriti.integrations.common.wake_runner import run
    except ImportError:
        return 0
    return run(script_dir=Path(__file__).parent)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

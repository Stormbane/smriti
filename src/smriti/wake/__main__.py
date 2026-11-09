"""``python -m smriti.wake``: the SessionStart hook's entry point.

Every harness's SessionStart hook runs ``<python> -P -m smriti.wake`` with
the python that installed smriti
(:mod:`smriti.integrations.common.hook_model`); ``-P`` keeps the current
directory off the import path. The code is the installed package's: nothing
in the memory tree is run, so a restore of the tree or a write to it can't
change what a session start runs. The work is
:func:`smriti.integrations.common.wake_runner.run`.

Environment knobs:

    SMRITI_WAKE=1|full|on|true       emit briefing (default off so
                                     ``claude -p`` etc. stay clean)
    SMRITI_ROOT=/path/to/entity      override entity tree location
    SMRITI_WAKE_FRAMING=raw          plain stdout (Claude Code default)
                       =codex-json   {"hookSpecificOutput": ...} (Codex)
    SMRITI_WAKE_AUDIENCE=coding      which briefing (the default)

A hook whose python has no smriti fails before this module runs: python
says on stderr that it can't find smriti, with exit status 1. If smriti is
there but its wake can't be imported, a briefing that was asked for is
missing: that is said in one line on stderr, naming the interpreter, with
exit status 1. Nothing goes to stdout, which the harness adds to the
session. Neither harness stops a session for a SessionStart hook that
fails: Claude Code shows a notice with stderr's first line (with status 0
it discards stderr), and Codex reports a hook failure. With SMRITI_WAKE off
no briefing was asked for, and it exits 0 in silence.
"""

from __future__ import annotations

import os
import sys

_ON = {"1", "full", "on", "true", "yes"}  # as wake_runner reads SMRITI_WAKE


def main() -> int:
    try:
        from smriti.integrations.common.wake_runner import run
    except ImportError as exc:
        if os.environ.get("SMRITI_WAKE", "").strip().lower() not in _ON:
            return 0
        sys.stderr.write(
            f"[wake] no memory briefing: {sys.executable or 'this python'} can't import "
            f"smriti's wake ({exc}); reinstall smriti for it, or re-run smriti's installer "
            "with the python that has it\n"
        )
        return 1
    return run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — never breaks SessionStart; reported on stderr
        sys.stderr.write(f"[wake] error: {exc}\n")
        sys.exit(0)

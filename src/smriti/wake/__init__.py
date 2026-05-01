"""Session-wake briefing — harness-neutral.

Provides ``briefing()`` returning the same identity + threads + project
context + recent-journal payload that ``narada/.smriti/wake.py`` emits
on Claude Code's SessionStart, but as a callable library function so
non-Claude-Code harnesses (Cursor, custom Python agents, Continue.dev)
can use the same briefing for their own session-start equivalents.

The harness-specific bits — SMRITI_WAKE env gating, the backup
subprocess kick, the ``last-activity`` touch — stay in the script.
This module is pure assembly: read files under the memory root,
respect a character budget, return a string.
"""

from __future__ import annotations

from smriti.wake.briefing import briefing

__all__ = ["briefing"]

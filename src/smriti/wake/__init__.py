"""Session-wake briefing — harness-neutral.

Provides ``briefing()`` returning the identity + threads + project
context + recent-journal payload that ``python -m smriti.wake`` (this
package's ``__main__``) emits on every harness's SessionStart, as a
callable library function so other harnesses (Cursor, custom Python
agents, Continue.dev) can use the same briefing for their own
session-start equivalents.

The hook's bits — SMRITI_WAKE env gating, the framing, the
``last-activity`` touch — are in
``smriti.integrations.common.wake_runner``. This module is pure
assembly: read files under the memory root, respect a character budget,
return a string.
"""

from __future__ import annotations

from smriti.wake.briefing import briefing

__all__ = ["briefing"]

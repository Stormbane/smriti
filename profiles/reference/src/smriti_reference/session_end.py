"""A UserPromptSubmit hook: when Suti's prompt sounds like the session is
ending, remind the agent to write down what's worth keeping first.

Run as ``python -P -m smriti_reference.session_end`` from the user's Claude
Code settings, where the reference installation wires it by hand. Claude Code
ignores a UserPromptSubmit hook's ``matcher``, so the prompt is matched here.
Stdout reaches the agent as context. The hook always exits 0, so a fault in it
never holds up the prompt.
"""

from __future__ import annotations

import json
import re
import sys

# The matcher the hook first had in settings.json. It also matches inside
# other sentences ("closing the bug", "restart the server"); tune it when a
# real miss is seen.
SESSION_END_PATTERN = re.compile(
    r"(?i)("
    r"bye|goodbye|done for now|end session|closing|shutting down|"
    r"restart|reboot|gotta go|heading out|wrapping up|that'?s all|"
    r"call it|signing off|good night|goodnight|talk later|see you|"
    r"ttyl|brb"
    r")"
)

REMINDER = """--- SESSION ENDING ---
Suti is wrapping up. Before responding, you MUST:
1. Use smriti_write to persist anything worth remembering (observations,
   decisions, session state, what to pick up next). Use branch "journal"
   for significant moments, "projects/{project}" for project-specific
   notes, or let the default handle it.
2. Update ~/.narada/ directly ONLY for identity-level shifts (mind.md,
   open-threads.md, suti.md, practices.md). These are trunk files — only
   change them when something actually shifted.
Do this BEFORE your final response to Suti."""


def reminder_for(payload: object) -> str | None:
    """The reminder when the hook payload's prompt sounds like goodbye, else None."""
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and SESSION_END_PATTERN.search(prompt):
        return REMINDER
    return None


def main() -> int:
    """Read Claude Code's payload on stdin and print the reminder if it applies."""
    try:
        payload = json.loads(sys.stdin.buffer.read() or b"null")
    except ValueError:  # not JSON, or not UTF-8: stay out of the way
        return 0
    text = reminder_for(payload)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001 — a hook fault must never hold up the prompt
        print(f"session_end: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 0
    sys.exit(code)

"""Secret scrub applied to every turn before persistence.

Session transcripts — unlike journal entries — can contain pasted
credentials. The day-log lives in a backed-up git repo, so obvious
secret shapes are masked at capture. This is a targeted scrub for
high-confidence credential patterns, not a PII framework (spec:
review round 1, finding 7).
"""

from __future__ import annotations

import re

_PATTERNS: list[re.Pattern[str]] = [
    # Anthropic / OpenAI / GitHub / Slack / AWS-style token literals.
    re.compile(r"\bsk-(?:ant|proj|live|test)?-?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Bearer headers and explicit assignments of secret-named vars.
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?i)\b((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)"
        r"[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?"
    ),
    # Private key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

_MASK = "[redacted]"


def scrub(text: str) -> str:
    """Mask credential-shaped substrings; passes ordinary text through."""
    out = text
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: f"{m.group(1)} {_MASK}".strip() + " ", out)
        else:
            out = pat.sub(_MASK, out)
    return out

"""Secret scrub applied to every turn before persistence.

Session transcripts, unlike journal entries, can contain pasted
credentials. The day-log lives in a backed-up git repo, so credential
shapes are masked at capture. This is a targeted scrub for
high-confidence credential patterns, not a PII framework (spec:
review round 1, finding 7): anything it doesn't recognise is kept.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_MASK = "[redacted]"


def _keep_prefix(m: re.Match[str]) -> str:
    """For 'name = secret' and 'Bearer secret': keep the name, mask the value."""
    return f"{m.group(1)} {_MASK}".strip() + " "


def _keep_url_parts(m: re.Match[str]) -> str:
    """scheme://user:password@host keeps everything but the password."""
    return f"{m.group(1)}{_MASK}@"


_Replacement = str | Callable[[re.Match[str]], str]

_PATTERNS: list[tuple[re.Pattern[str], _Replacement]] = [
    # Provider token literals: Anthropic / OpenAI (sk-...), GitHub, Slack, AWS.
    (re.compile(r"\bsk-(?:ant|proj|live|test)?-?[A-Za-z0-9_-]{16,}\b"), _MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), _MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), _MASK),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), _MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _MASK),
    # Stripe secret and restricted keys, Google API keys, npm tokens.
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), _MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), _MASK),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), _MASK),
    # Telegram bot tokens (<bot id>:<35-char secret>) and Slack webhooks.
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), _MASK),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), _MASK),
    # JSON Web Tokens: three base64url parts, the first two starting "eyJ".
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), _MASK),
    # Credentials embedded in URLs: postgres://user:password@host.
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.IGNORECASE), _keep_url_parts),
    # Bearer headers and explicit assignments of secret-named vars.
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), _keep_prefix),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)"
            r"[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?"
        ),
        _keep_prefix,
    ),
    # Private key blocks.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     _MASK),
]


def scrub(text: str) -> str:
    """Mask credential-shaped substrings; passes ordinary text through."""
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out

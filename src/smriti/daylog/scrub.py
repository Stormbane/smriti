"""Secret scrub applied to every turn, and every memory write, before persistence.

Session transcripts can contain pasted credentials, and so can what an
agent writes to memory: it writes down what it was shown. The tree lives
in a backed-up git repo, so credential shapes are masked at capture
(``smriti.daylog.writer``) and on every write of memory text into the
tree (``smriti.core.memwrite``). This is a targeted scrub for
high-confidence credential patterns, not a PII framework: anything it
doesn't recognise is kept.

Each pattern has a hint: a cheap test that holds wherever the pattern can
match (a literal it requires, or a short search that starts with one). A
pattern runs only where its hint holds, so the result is the pattern's
alone, but ordinary text costs a few substring searches instead of a
regex tried at every position (a pattern that starts with ``\\b`` gets
no literal scan from the engine: about 1 ms per 50 kB each, 19 ms for
the lot). For the same reason a pattern that starts with ``\\b`` and a
literal is written literal first (:func:`_word_start`). ``tests/test_scrub.py``
holds the result to the scrub's before either change, pattern for pattern.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

_MASK = "[redacted]"


def _keep_prefix(m: re.Match[str]) -> str:
    """For 'name = secret' and 'Bearer secret': keep the name, mask the value."""
    return f"{m.group(1)} {_MASK}".strip() + " "


def _keep_url_parts(m: re.Match[str]) -> str:
    """scheme://user:password@host keeps everything but the password."""
    return f"{m.group(1)}{_MASK}@"


_Replacement = str | Callable[[re.Match[str]], str]
# A hint gets the text and its lower case, or None for the lower case when
# lower() can't stand in for IGNORECASE (see _folded).
_Hint = Callable[[str, str | None], bool]

# The only characters outside ASCII that IGNORECASE matches to an ASCII letter
# (U+0130 and U+0131 to i, U+017F to s, U+212A to k); tests/test_scrub.py
# checks the list against every code point.
_FOLDS_TO_ASCII = re.compile("[\u0130\u0131\u017f\u212a]")


def _folded(text: str) -> str | None:
    """*text* in lower case, where a case-insensitive ASCII pattern matches *text*
    exactly where its lower-case form matches this; None where it can't be sure."""
    if text.isascii() or not _FOLDS_TO_ASCII.search(text):
        return text.lower()
    return None


def _has(*literals: str) -> _Hint:
    """The pattern needs one of *literals*, case and all."""
    return lambda text, _lower: any(lit in text for lit in literals)


def _finds(regex: str) -> _Hint:
    """The pattern needs a match of *regex* (which starts with a literal, so is fast)."""
    search = re.compile(regex).search
    return lambda text, _lower: search(text) is not None


def _folded_has(*regexes: str) -> _Hint:
    """For a case-insensitive pattern: one of *regexes* matches the lower-cased text.

    When there is no lower case to search (:func:`_folded`), the pattern runs.
    """
    searches = [re.compile(regex).search for regex in regexes]
    return lambda _text, lower: lower is None or any(s(lower) for s in searches)


def _word_start(head: str) -> str:
    r"""``\b`` then *head* (fixed width), written so the engine can scan for *head*.

    The look back checks the boundary before *head* once *head* has matched,
    so the matches are the same, and the search about 30 times faster.
    """
    return rf"{head}(?<=\b{head})"


class _Rule(NamedTuple):
    pattern: re.Pattern[str]
    replacement: _Replacement
    hint: _Hint


_RULES: list[_Rule] = [
    # Provider token literals: Anthropic / OpenAI (sk-...), GitHub, Slack, AWS.
    _Rule(re.compile(_word_start("sk-") + r"(?:ant|proj|live|test)?-?[A-Za-z0-9_-]{16,}\b"),
          _MASK, _has("sk-")),
    _Rule(re.compile(_word_start("gh[pousr]_") + r"[A-Za-z0-9]{20,}\b"),
          _MASK, _finds(r"gh[pousr]_")),
    _Rule(re.compile(_word_start("github_pat_") + r"[A-Za-z0-9_]{22,}\b"),
          _MASK, _has("github_pat_")),
    _Rule(re.compile(_word_start("xox[baprs]-") + r"[A-Za-z0-9-]{10,}\b"), _MASK, _has("xox")),
    _Rule(re.compile(_word_start("AKIA") + r"[0-9A-Z]{16}\b"), _MASK, _has("AKIA")),
    # Stripe secret and restricted keys, Google API keys, npm tokens.
    _Rule(re.compile(_word_start("[sr]k_") + r"(?:live|test)_[A-Za-z0-9]{16,}\b"), _MASK,
          _has("k_live_", "k_test_")),
    _Rule(re.compile(_word_start("AIza") + r"[0-9A-Za-z_-]{35}\b"), _MASK, _has("AIza")),
    _Rule(re.compile(_word_start("npm_") + r"[A-Za-z0-9]{36}\b"), _MASK, _has("npm_")),
    # Telegram bot tokens (<bot id>:<35-char secret>) and Slack webhooks.
    _Rule(re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), _MASK, _finds(r":[A-Za-z0-9_-]{35}")),
    _Rule(re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), _MASK,
          _has("https://hooks.slack.com/services/")),
    # JSON Web Tokens: three base64url parts, the first two starting "eyJ".
    _Rule(re.compile(_word_start("eyJ") + r"[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}"
                     r"\.[A-Za-z0-9_-]{8,}"), _MASK, _has("eyJ")),
    # Credentials embedded in URLs: postgres://user:password@host. The hint is
    # the pattern from "://" on; none of its classes holds a letter, so case
    # doesn't matter to it.
    _Rule(re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.IGNORECASE),
          _keep_url_parts, _finds(r"://[^/\s:@]+:[^@\s/]+@")),
    # Bearer headers and explicit assignments of secret-named vars.
    _Rule(re.compile(r"(?i)(" + _word_start("b") + r"earer)\s+[A-Za-z0-9._~+/=-]{16,}"),
          _keep_prefix, _folded_has("bearer")),
    _Rule(
        re.compile(
            r"(?i)\b((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)"
            r"[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?"
        ),
        _keep_prefix,
        # Every name ends in one of these, and the name is followed by = or :.
        _folded_has(r"key\s*[=:]", r"token\s*[=:]", r"secret\s*[=:]", r"passw(?:or)?d\s*[=:]"),
    ),
    # Private key blocks.
    _Rule(re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
          _MASK, _has("-----BEGIN ")),
]


def scrub(text: str) -> str:
    """Mask credential-shaped substrings; passes ordinary text through."""
    out = text
    folded: str | None = None
    fresh = False  # whether ``folded`` is the lower case of ``out``
    for pattern, replacement, hint in _RULES:
        if not fresh:
            folded, fresh = _folded(out), True
        if not hint(out, folded):
            continue
        out, count = pattern.subn(replacement, out)
        fresh = fresh and not count
    return out

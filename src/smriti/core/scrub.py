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

Assignments to a secret's name come in two forms, each its own rule. A
bare name (``password = x``, ``token: x``) is the name alone, as a word.
A quoted key, as JSON, YAML, TOML and Python write one
(``{"password": "x"}``, ``'token':'x'``, ``\\"secret\\": \\"x\\"`` inside
escaped JSON, and deeper), may also end in the name (``access_token``,
``client-secret``, ``db.password``, ``accessToken``), but a key that only
begins with one (``password_hint``, ``token_type``) isn't a secret's. A
quoted value is masked whole, escapes, quotes and spaces included, up to
the quote that closes it at its own level of escaping, on the same line;
an unquoted or unterminated one as far as
the bare form's characters go. Either form needs a value of at least 8
characters, so ``"password": ""`` and ``token: none`` are kept. Not
masked: a secret under a key that isn't a secret's name
(``{"password": {"value": "x"}}``), and a bare name's quoted value with
other characters (``password = "p@ss word"``), which the bare form's value
rule doesn't reach. The two rules share one scan of the text
(:func:`_assignment_forms`), so ordinary text pays for one hint, not two.
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


def _keep_key_and_quotes(m: re.Match[str]) -> str:
    """For '"name": "secret"': keep the key and the value's quotes, mask what they enclose."""
    quote = (m.group("vesc") or "") + m.group("vq") if m.group("vq") else ""
    return f"{m.string[m.start():m.start('value')]}{quote}{_MASK}{quote}"


_Replacement = str | Callable[[re.Match[str]], str]
# Work that hints share within one scrub: the assignment forms (see
# _assignment_forms), with the text they were found in.
_Memo = dict[str, tuple[str, bool, bool]]
# A hint gets the text, its lower case (or None when lower() can't stand in
# for IGNORECASE, see _folded) and the scrub's memo.
_Hint = Callable[[str, str | None, _Memo], bool]

# The only characters outside ASCII that IGNORECASE matches to an ASCII letter
# (U+0130 and U+0131 to i, U+017F to s, U+212A to k); tests/test_scrub.py
# checks the list against every code point.
_FOLDS_TO_ASCII = re.compile("[İıſK]")


def _folded(text: str) -> str | None:
    """*text* in lower case, where a case-insensitive ASCII pattern matches *text*
    exactly where its lower-case form matches this; None where it can't be sure."""
    if text.isascii() or not _FOLDS_TO_ASCII.search(text):
        return text.lower()
    return None


def _has(*literals: str) -> _Hint:
    """The pattern needs one of *literals*, case and all."""
    return lambda text, _lower, _memo: any(lit in text for lit in literals)


def _finds(regex: str) -> _Hint:
    """The pattern needs a match of *regex* (which starts with a literal, so is fast)."""
    search = re.compile(regex).search
    return lambda text, _lower, _memo: search(text) is not None


def _folded_has(*regexes: str) -> _Hint:
    """For a case-insensitive pattern: one of *regexes* matches the lower-cased text.

    When there is no lower case to search (:func:`_folded`), the pattern runs.
    """
    searches = [re.compile(regex).search for regex in regexes]
    return lambda _text, lower, _memo: lower is None or any(s(lower) for s in searches)


def _word_start(head: str) -> str:
    r"""``\b`` then *head* (fixed width), written so the engine can scan for *head*.

    The look back checks the boundary before *head* once *head* has matched,
    so the matches are the same, and the search about 30 times faster.
    """
    return rf"{head}(?<=\b{head})"


# A secret's name, for both assignment forms.
_SECRET_NAME = r"(?:api[_-]?key|token|secret|password|passwd)"


def _ends_in_name() -> str:
    r"""Look-backs that hold where a quoted key ends in a secret's name.

    The name is the whole key (after its opening quote), or follows ``_``,
    ``-`` or ``.``, or starts a camel-case hump (``accessToken``: a lower-case
    letter or a digit, then the name's capital). Its case is ignored, in
    ASCII only. Each look-back is fixed width, so ``api[_-]?key`` is spelled
    out three ways.
    """
    looks = []
    for name in ("apikey", "api_key", "api-key", "token", "secret", "password", "passwd"):
        looks.append(rf"(?<=[\"'_.-](?ai:{name}))")
        looks.append(rf"(?<=[a-z0-9]{name[0].upper()}(?ai:{name[1:]}))")
    return "(?:" + "|".join(looks) + ")"

# Every name ends in one of these (lower case), and either form follows it
# with = or :, a quoted key's closing quote between (after its level's
# backslashes). Group 1 is that quote.
# The API key's is "key" with "api" looked back for: the engine scans for a
# literal's first letter, and an "a" is everywhere (42 us per 50 kB, not 16).
_ASSIGNMENT_SEARCHES = [
    re.compile(end + r"(\\*[\"'])?\s*[=:]").search
    for end in (r"key(?:(?<=apikey)|(?<=api[_-]key))", "token", "secret", r"passw(?:or)?d")
]


def _assignment_forms(text: str, lower: str | None, memo: _Memo) -> tuple[bool, bool]:
    """Whether *text* may hold a quoted key's assignment, and a bare name's.

    One scan answers both rules; the second rule reuses the answer while the
    text is the one scanned (the first rule masked nothing).
    """
    if lower is None:
        return True, True
    seen = memo.get("assignments")
    if seen is not None and seen[0] is text:
        return seen[1], seen[2]
    quoted = bare = False
    for search in _ASSIGNMENT_SEARCHES:
        m = search(lower)
        while m is not None and not (quoted and bare):
            if m.group(1):
                quoted = True
            else:
                bare = True
            m = search(lower, m.end())
        if quoted and bare:
            break
    memo["assignments"] = (text, quoted, bare)
    return quoted, bare


def _quoted_key_hint(text: str, lower: str | None, memo: _Memo) -> bool:
    return _assignment_forms(text, lower, memo)[0]


def _bare_name_hint(text: str, lower: str | None, memo: _Memo) -> bool:
    # The quoted-key rule, which runs between the two hints, never makes a
    # bare name's assignment (it keeps each key and its closing quote), so a
    # text that held none before it holds none after.
    seen = memo.get("assignments")
    if seen is not None and not seen[2]:
        return False
    return _assignment_forms(text, lower, memo)[1]


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
    # Bearer headers.
    _Rule(re.compile(r"(?i)(" + _word_start("b") + r"earer)\s+[A-Za-z0-9._~+/=-]{16,}"),
          _keep_prefix, _folded_has("bearer")),
    # A quoted key that is, or ends in, a secret's name: the key (its quote,
    # maybe escaped, and the same one closing it) with = or :, then the value:
    # quoted (escapes kept inside it), quoted inside escaped JSON, or bare.
    # Before the bare form, so `token: "password": "x"` masks x, not the key.
    # For speed, the pattern starts with a bare quote, which the engine scans
    # for (inside a group, after an optional backslash or under a global
    # IGNORECASE, it is tried at every position: 0.2-1.1 ms per 50 kB);
    # which quote, and its escape, are looked back at. The key is taken
    # whole, without backtracking, and its end looked back at too
    # (:func:`_ends_in_name`, the only part that ignores case).
    #
    # A value's escaping has a level: JSON is 0, JSON in a JSON string 1, and
    # so on. The value opens with its level's run of backslashes (``vesc``,
    # none at level 0) and its quote, and ends with the same. Inside it, a
    # run of backslashes is read as escaped backslashes (the run and a
    # backslash, twice), then an escaped quote (the run, a backslash, the
    # run, the quote), or, before any other character, as the value's
    # (``\n``, an outer level's ``\u00e9``); then any character but the
    # quote. None of these reaches the end, and each is tried in that order,
    # so the value is read once, without backtracking (the pairs first, or a
    # long run was scanned again at every pair), and a quote inside it, at
    # any level, never ends it.
    _Rule(
        re.compile(
            r"[\"'](?<=(?P<kq>[\"']))"
            r"[A-Za-z0-9_.-]*+(?=\\*(?P=kq)\s*[=:])" + _ends_in_name()
            + r"\\*(?P=kq)\s*[=:]\s*"
            r"(?P<value>(?P<vesc>\\*)(?P<vq>[\"'])"
            r"(?:(?P=vesc)\\(?P=vesc)\\|(?P=vesc)\\(?P=vesc)(?P=vq)"
            r"|\\++(?!(?P=vq))(?=[^\\\n])|(?!(?P=vq))[^\\\n]){8,}+(?P=vesc)(?P=vq)"
            r"|[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?)"
        ),
        _keep_key_and_quotes,
        _quoted_key_hint,
    ),
    # A bare name: explicit assignments of secret-named vars.
    _Rule(
        re.compile(r"(?i)\b(" + _SECRET_NAME + r"\s*[=:]\s*)[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?"),
        _keep_prefix,
        _bare_name_hint,
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
    memo: _Memo = {}
    for pattern, replacement, hint in _RULES:
        if not fresh:
            folded, fresh = _folded(out), True
        if not hint(out, folded, memo):
            continue
        out, count = pattern.subn(replacement, out)
        fresh = fresh and not count
    return out

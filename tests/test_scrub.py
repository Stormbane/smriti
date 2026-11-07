"""The faster scrub masks exactly what its patterns do, run plainly (H12.1), and
quoted keys are masked as well (H13.4).

Each pattern runs only where its hint holds, and the patterns that start
with a word boundary and a literal are written literal first, both for
speed. A hint that failed to hold where its pattern matches, or a rewrite
that matched differently, would leave a credential in the tree. These hold
the scrub to its patterns as first written, every one run on every text
(copied below: the patterns before H12.1, and the quoted-key rule added
after it), over the scrub's own samples in many settings and a seeded fuzz
of fragments chosen to sit on the edges of each pattern and its hint. The
patterns before H12.1 alone stay the baseline wherever the quoted-key rule
finds nothing. The credentials are synthetic, split so no scanner takes
them for real ones.
"""

from __future__ import annotations

import random
import re
import sys
import time

import pytest

from smriti.daylog import scrub as scrub_mod
from smriti.daylog.scrub import scrub

_SAMPLES = [
    "sk-ant-api03-abcdefghijklmnop1234",
    "sk-" + "proj-ABCDEFGHIJKLMNOPqrst",
    "ghp_ABCDEFGHIJKLMNOPQRST1234",
    "ghs_" + "abcdefghijklmnopqrstuvwxyz",
    "xoxb-1234567890-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "api_key = supersecret42",
    "API-KEY: 'abcdefgh12345'",
    "password: hunter2hunter2",
    "Passwd=\"abcdefgh99\"",
    "token:abcdefgh.ijkl",
    "SECRET = abc/def+ghi~jk",
    "github_pat_" + "11ABCDEFG0123456789_abcdefghij",
    "sk_" + "live_" + "51HxABCDEFGHIJKLMNOPQRSTUV",
    "rk_" + "test_" + "51HxABCDEFGHIJKLMNOPQRSTUV",
    "AIza" + "SyA1234567890abcdefghijklmnopqrstuv",
    "npm_" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawB",
    "https://hooks.slack.com/" + "services/T000/B000/XXXXXXXXXXXXXXXX",
    "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "postgres://app:s3cr3tpass@db.example.com:5432/main",
    "HTTPS://Sam:hunter2@Example.com/x",
    "Authorization: Bearer abcdefghijklmnop1234",
    "BEARER ABCDEFGHIJKLMNOPQRST",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX\n"
    "-----END RSA PRIVATE KEY-----",
    # Quoted keys (H13.4): JSON, Python and YAML, escaped JSON, a key ending in a name.
    '{"password": "synthetic-' + 'secret-12345"}',
    '"token":"abcdefghij' + 'klmnopqrst"',
    "{'api_key': 'abc def " + "ghi jkl'}",
    '{\\"secret\\": \\"abcdef' + 'gh99\\"}',
    '"access_token" : "abcdefgh' + 'ijklmnop"',
    '"clientSecret": "p@ss w0rd!' + '!"',
]

# Pieces that sit on the edges: a pattern's literal without the rest, the
# rest without the literal, case variants, letters IGNORECASE folds that
# lower() doesn't (ſ, ı, K), and the separators the patterns care about.
_FRAGMENTS = [
    *_SAMPLES, "sk-", "sk-ant-", "gh", "ghp_", "gho_short", "github_pat_", "xox", "xoxp-",
    "AKIA", "akia", "k_live_", "k_test_", "AIza", "npm_", "eyJ", "eyJabc.eyJdef.", "://",
    "@", ":", "=", " = ", "'", '"', "/", "-", "_", ".", "~", "+", "\n", "\r\n", "\t", " ",
    "bearer", "Bearer", "BEARER", "bear", "token", "TOKEN", "Token", "tokens", "secret",
    "Secret", "password", "PASSWORD", "passwd", "pass", "api", "api_key", "apikey", "api-key",
    "KEY", "key", "ſecret", "paſſwd", "toKen", "bearır", "beareı", "ſk-",
    "12345678", "1234567890", "12:30", "2026-11-06", "abcdefghijklmnopqrstuvwxyz0123456789",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789abcdef", "postgres", "https", "user:pw",
    "db.example.com", "-----BEGIN ", "PRIVATE KEY-----", "-----END ", "[redacted]", "é", "स्मृ",
    # Quoted keys' edges: escapes, both quotes, separators, prefixes and suffixes.
    "\\", '\\"', "\\'", '""', '": "', "': '", '":"', " : ", '"password"', "'token'",
    '"api_key"', '\\"secret\\"', "access_", "client-", "db.", "access", "Token", "SECRET",
    "_hint", "_type", "apikey", "x-api-key", "{", "}", ", ", "ApiKey", "Passwd", "x9", "API_",
    "api", "Api-Key",
]


# The scrub's patterns before H12.1, in order, with their replacements.
_BEFORE: list[tuple[re.Pattern[str], object]] = [
    (re.compile(r"\bsk-(?:ant|proj|live|test)?-?[A-Za-z0-9_-]{16,}\b"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "[redacted]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[redacted]"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     "[redacted]"),
    (re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.IGNORECASE),
     scrub_mod._keep_url_parts),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), scrub_mod._keep_prefix),
    (re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)"
                r"[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?"), scrub_mod._keep_prefix),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[redacted]"),
]

# The quoted-key rule (H13.4) as first written, and where it runs: before the bare
# names'. The scrub's takes the key whole and looks back at its end, for speed.
_QUOTED_KEY = re.compile(
    r"(?P<key>(?P<kq>\\?[\"'])"
    r"(?:[A-Za-z0-9_.-]*(?:[_.-]|[a-z0-9](?=[A-Z])))?"
    r"(?ai:api[_-]?key|token|secret|password|passwd)(?P=kq)\s*[=:]\s*)"
    r"(?:(?P<vq>[\"'])(?:\\.|(?!(?P=vq))[^\\\n]){8,}(?P=vq)"
    r"|(?P<evq>\\[\"'])(?:(?!(?P=evq))[^\n]){8,}(?P=evq)"
    r"|[\"']?[A-Za-z0-9._~+/-]{8,}[\"']?)"
)
_AT = 13


def _keep_key(m: re.Match[str]) -> str:
    quote = m.group("vq") or m.group("evq") or ""
    return f"{m.group('key')}{quote}[redacted]{quote}"


_NOW = [*_BEFORE[:_AT], (_QUOTED_KEY, _keep_key), *_BEFORE[_AT:]]


def _plain(text: str, patterns: list[tuple[re.Pattern[str], object]] = _NOW) -> str:
    """Every pattern, as it was written, on every text."""
    out = text
    for pattern, replacement in patterns:
        out = pattern.sub(replacement, out)  # type: ignore[call-overload]
    return out


def _texts() -> list[str]:
    texts = [f"context {s} more" for s in _SAMPLES]
    texts += [s for s in _SAMPLES] + ["".join(_SAMPLES), " ".join(_SAMPLES)]
    texts += [f"{a}\n{b}" for a in _SAMPLES[:6] for b in _SAMPLES[-6:]]
    rng = random.Random(1206)
    for _ in range(4000):
        texts.append("".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 12))))
    return texts


def test_the_scrub_masks_what_its_patterns_do_on_every_text() -> None:
    texts = _texts()
    assert len(_NOW) == len(scrub_mod._RULES)
    differ = [t for t in texts if scrub(t) != _plain(t)]
    assert not differ, differ[:5]
    assert sum(scrub(t) != t for t in texts) > 500  # the fuzz does reach the patterns


def test_the_scrub_masks_what_it_did_before_wherever_no_quoted_key_is_found() -> None:
    """The quoted-key rule changes only what it matches: a text it finds nothing in, as
    the rules before it leave the text, is masked exactly as before it was added."""
    changed = 0
    for text in _texts():
        if _plain(text) != _plain(text, _BEFORE):
            assert _QUOTED_KEY.search(_plain(text, _BEFORE[:_AT])), text
            changed += 1
    assert changed > 100  # the fuzz does reach the quoted keys


def test_only_the_listed_characters_fold_to_ascii_letters() -> None:
    """The case-insensitive hints search the lower case unless the text holds
    a character IGNORECASE matches to an ASCII letter while lower() doesn't make
    it one; every such character must be in the list."""
    every = "".join(chr(c) for c in range(sys.maxunicode + 1) if not 0xD800 <= c <= 0xDFFF)
    folding = {c for c in re.findall(r"(?i)[a-z]", every) if not c.isascii()}
    listed = set(scrub_mod._FOLDS_TO_ASCII.pattern.strip("[]"))
    assert folding <= listed, sorted(map(hex, map(ord, folding - listed)))


@pytest.mark.parametrize("secret", _SAMPLES)
def test_every_sample_is_masked_and_masking_is_stable(secret: str) -> None:
    masked = scrub(f"Sam said {secret} to Ari")
    assert "[redacted]" in masked and masked != f"Sam said {secret} to Ari"
    assert scrub(masked) == masked


# ---------------------------------------------------------------- quoted keys (H13.4)

_NESTED = ('{"db": {"user": "sam", "password": "synthetic-' + 'pass-123", "port": 5432},\n'
           ' "auth": {"Token": "abcdefgh' + 'ijklmnop", "scopes": ["read", "write"]}}')


@pytest.mark.parametrize(("text", "masked"), [
    # JSON, Python, YAML and TOML: either quote, any spacing, any case.
    ('{"password": "synthetic-' + 'secret-12345"}', '{"password": "[redacted]"}'),
    ('"token":"abcdefghij' + 'klmnopqrst"', '"token":"[redacted]"'),
    ("{'Password': 'abc def " + "ghi jkl'}", "{'Password': '[redacted]'}"),
    ('"API_KEY" :  "abcdefgh' + '12345"', '"API_KEY" :  "[redacted]"'),
    ('"PASSWD"="abcdefgh' + '12345"', '"PASSWD"="[redacted]"'),
    ('"password" = "correct horse ' + 'battery"', '"password" = "[redacted]"'),
    ("'secret': abcdefgh" + "12345", "'secret': [redacted]"),
    ('"token": "abcdefgh\'' + 'ijklmn"', '"token": "[redacted]"'),  # the other quote inside
    # Nested JSON: each secret masked, its neighbours kept.
    (_NESTED, '{"db": {"user": "sam", "password": "[redacted]", "port": 5432},\n'
              ' "auth": {"Token": "[redacted]", "scopes": ["read", "write"]}}'),
    ('{"token":"aaaaaaaa' + 'aa","password":"bbbbbbbb' + 'bb"}',
     '{"token":"[redacted]","password":"[redacted]"}'),
    # A key that ends in a secret's name: snake, kebab, dotted and camel case.
    ('{"access_token": "abcdefgh' + 'ijklmnop", "token_type": "Bearer"}',
     '{"access_token": "[redacted]", "token_type": "Bearer"}'),
    ('{"refreshToken": "abcdefgh' + 'ijklmnop"}', '{"refreshToken": "[redacted]"}'),
    ('"client-secret": "abcdefgh' + 'ijk"', '"client-secret": "[redacted]"'),
    ('"db.password": "abcdefgh' + 'ijk"', '"db.password": "[redacted]"'),
    ('"x-api-key": "abcdefgh' + 'ijk"', '"x-api-key": "[redacted]"'),
    # Escaped quotes: inside a value, and JSON inside a JSON string (a curl -d, a log).
    ('{"password": "abc\\"def' + '12345"}', '{"password": "[redacted]"}'),
    ('{"password": "a\\\\b\\nc' + 'defgh"}', '{"password": "[redacted]"}'),
    ('-d "{\\"password\\": \\"hunter2' + 'hunter2\\"}"', '-d "{\\"password\\": \\"[redacted]\\"}"'),
    # A value with no closing quote on its line: as far as a bare name's value goes.
    ('"password": "abcdefgh' + '12345\nnext line', '"password": [redacted]\nnext line'),
])
def test_quoted_keys_are_masked(text: str, masked: str) -> None:
    assert scrub(text) == masked
    assert scrub(masked) == masked


@pytest.mark.parametrize("text", [
    '{"password": ""}',  # nothing to mask
    '{"password": "short"}',  # under 8 characters, as for a bare name
    '{"password": null, "token": true}',
    '{"password_hint": "the cat sat on the mat"}',  # begins with a secret's name
    '{"token_type": "refresh-or-access"}',
    '{"max_tokens": 1234567890}',
    '{"secret_name": "prod-database-main"}',
    '{"key": "a-cache-key-value", "primary_key": "user-id-column"}',  # "key" isn't api_key
    '"password\': \'abcdefgh12345\'',  # the key's quotes differ: not a key
])
def test_what_quoted_keys_keep(text: str) -> None:
    assert scrub(text) == text


def test_a_long_quoted_value_is_masked_whole() -> None:
    value = "abcdefghij" * 2000
    masked = scrub(f'{{"token": "{value}", "user": "sam"}}')
    assert masked == '{"token": "[redacted]", "user": "sam"}'
    # Unterminated, or all escapes: no closing quote to find, and no long backtracking.
    start = time.perf_counter()
    assert scrub('"password": "' + value) == '"password": [redacted]'
    scrub('"password": "' + "\\" * 20_000)
    scrub('\\"password\\": \\"' + value)
    assert time.perf_counter() - start < 1.0


def _seconds(work: object, text: str, reps: int = 10) -> float:
    assert callable(work)
    start = time.perf_counter()
    for _ in range(reps):
        work(text)
    return (time.perf_counter() - start) / reps


def test_ordinary_text_costs_a_fraction_of_the_plain_scrub() -> None:
    """The hints' point: 50 kB without a credential took 19 ms here; now about 0.6."""
    rng = random.Random(7)
    words = ["Ari", "Sam", "nightly", "digest", "index", "token", "search", "12:30",
             "https://example.com/a/b", "path/to/file.py", "a@b.c", "\n", "- item"]
    text = " ".join(rng.choice(words) for _ in range(8000))[:50_000]
    assert scrub(text) == text == _plain(text)
    assert _seconds(scrub, text) < _seconds(_plain, text) / 5

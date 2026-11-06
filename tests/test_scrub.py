"""The faster scrub masks exactly what the scrub before it did (H12.1).

Each pattern runs only where its hint holds, and the patterns that start
with a word boundary and a literal are written literal first, both for
speed. A hint that failed to hold where its pattern matches, or a rewrite
that matched differently, would leave a credential in the tree. These hold
the scrub to the one before both changes (its patterns are copied below),
over the scrub's own samples in many settings and a seeded fuzz of
fragments chosen to sit on the edges of each pattern and its hint. The
credentials are synthetic, split so no scanner takes them for real ones.
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


def _plain(text: str) -> str:
    """The scrub before H12.1: every pattern, as it was written, on every text."""
    out = text
    for pattern, replacement in _BEFORE:
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


def test_the_scrub_masks_what_it_did_before() -> None:
    texts = _texts()
    assert len(_BEFORE) == len(scrub_mod._RULES)
    differ = [t for t in texts if scrub(t) != _plain(t)]
    assert not differ, differ[:5]
    assert sum(scrub(t) != t for t in texts) > 500  # the fuzz does reach the patterns


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

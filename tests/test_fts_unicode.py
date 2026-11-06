"""H12.3: keyword search keeps terms outside ASCII.

Outside review #5: each query word was stripped to ``[A-Za-z0-9_]``, so
``café`` became ``caf`` and a Devanagari or CJK word nothing; a query of such
words found nothing by keyword, and nothing at all in keyword-only mode.
A word with a letter or digit outside ASCII is now kept, quoted for FTS5,
which splits and folds it as it did the indexed text. ASCII queries ask for
exactly what they did before (the parity test at the end).
"""

from __future__ import annotations

import random
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from smriti.store.schema import IndexDB, ensure_schema, open_readonly
from smriti.store.search import _STOPWORDS, fts_match, fts_terms, search

DOCS = {
    "notes/cafe.md": "Le café du coin ouvre à sept heures.",
    "notes/smriti.md": "स्मृति का अर्थ है याद।",
    "notes/kioku.md": "記憶、それは大切なもの。",
    "notes/creme.md": 'The dessert was crème "brûlée", and naïve résumé writing.',
    "notes/near.md": "The lantern stood near the door, and or else nothing.",
    "notes/plain.md": "The nightly digest of the day log, e.g. its rollups.",
    "notes/sam.md": "Sam's notes on the nightly digest, with Ari.",
}


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[IndexDB]:
    """An index holding ``DOCS``, built through the schema: no embedder is needed."""
    db_path = tmp_path / "index.db"
    conn = ensure_schema(db_path, None)
    if not conn.has_fts:
        conn.close()
        pytest.skip("FTS5 not available")
    for i, (source, text) in enumerate(DOCS.items()):
        conn.execute(
            "INSERT INTO chunks (id, source, heading, heading_level, content, start_line, "
            "end_line, content_hash, trunk_distance, indexed_at) "
            "VALUES (?, ?, '', 0, ?, 1, 1, ?, 1, '2026-11-06')",
            (f"c{i}", source, text, f"h{i}"))
    conn.commit()
    conn.close()
    reader = open_readonly(db_path)
    yield reader
    reader.close()


def found(db: IndexDB, query: str) -> list[str]:
    return [r.source for r in search(db, query, use_vectors=False, use_reranker=False)]


@pytest.mark.parametrize(("query", "source"), [
    ("café", "notes/cafe.md"),
    ("CAFÉ", "notes/cafe.md"),
    ("स्मृति", "notes/smriti.md"),
    ("記憶", "notes/kioku.md"),
    ("naïve", "notes/creme.md"),
    ("résumé", "notes/creme.md"),
    ('"café"', "notes/cafe.md"),  # the word in quotes
    ('crème"brûlée', "notes/creme.md"),  # a quote inside the word: doubled, never syntax
    ("«café»", "notes/cafe.md"),
    ("(स्मृति)", "notes/smriti.md"),
])
def test_a_keyword_only_query_in_another_script_is_found(
        db: IndexDB, query: str, source: str) -> None:
    assert found(db, query)[:1] == [source]


@pytest.mark.parametrize("query", [
    "AND", "OR", "NOT", "NEAR", "NEAR(lantern door)", "lantern AND door", "lantern OR",
    'lantern"', '"', '""', "*", "lantern*", "content:lantern", "^lantern", "-lantern", "+",
    "(", ")", "{lantern}", "lantern:", "café AND", "NEAR(café", 'café"', "*café", "a.b:c(d)*",
    "e.g.", "it's", "claude -p 120s", "\\", "'", "--", "—", "“lantern”",
    'स्मृति" OR "x', "記憶*", "café:", "^café", "NEAR(स्मृति, 2)",
])
def test_operator_words_and_punctuation_are_safe(db: IndexDB, query: str) -> None:
    terms = fts_terms(db, query)
    if terms:  # MATCH raises on a syntax error, where search only logs it
        db.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                   (fts_match(terms),)).fetchall()
    found(db, query)


@pytest.mark.parametrize(("query", "source"), [
    ("NEAR lantern", "notes/near.md"),
    ("lantern AND door", "notes/near.md"),
    ("NEAR(lantern door)", "notes/near.md"),
    ("lantern*", "notes/near.md"),
    ('"nightly"', "notes/plain.md"),
    ("café AND", "notes/cafe.md"),
    ("NEAR café", "notes/cafe.md"),
    ('café"', "notes/cafe.md"),
    ("記憶*", "notes/kioku.md"),
    ('स्मृति" OR "x', "notes/smriti.md"),
])
def test_operator_words_and_punctuation_still_find(
        db: IndexDB, query: str, source: str) -> None:
    assert source in found(db, query)


def test_a_rare_term_in_another_script_survives_a_long_query(db: IndexDB) -> None:
    """Past 12 terms the rarest are kept, by the vocabulary: a word in another
    script is looked up as the tokenizer splits and folds it, not as typed."""
    filler = " ".join(f"filler{i}" for i in range(14))
    for word, source in (("स्मृति", "notes/smriti.md"), ("CAFÉ", "notes/cafe.md"),
                         ("記憶", "notes/kioku.md")):
        query = f"nightly digest {filler} {word}"
        assert word.lower() in fts_terms(db, query)
        assert source in found(db, query)


def test_a_short_word_in_another_script_is_a_term() -> None:
    conn = sqlite3.connect(":memory:")
    assert fts_terms(conn, "the 記憶 of it") == ["記憶"]
    assert fts_terms(conn, "心") == ["心"]


def test_a_quote_in_a_term_is_doubled() -> None:
    assert fts_match(['crème"brûlée', "café"]) == '"crème""brûlée" OR "café"'


# ── Parity ────────────────────────────────────────────────────────────


def fts_terms_before_h12_3(conn: sqlite3.Connection, query: str, k: int = 12) -> list[str]:
    """The rule every query followed before H12.3 (``a8a7dcb``), which ASCII queries still do."""
    raw = [re.sub(r"[^A-Za-z0-9_]", "", t) for t in query.strip().split()]
    raw = [t for t in raw if t]
    seen: set[str] = set()
    content: list[str] = []
    for t in raw:
        tl = t.lower()
        if len(tl) >= 3 and tl not in _STOPWORDS and tl not in seen:
            seen.add(tl)
            content.append(tl)
    if not content:
        return list(dict.fromkeys(t.lower() for t in raw))[:k]
    if len(content) <= k:
        return content
    df: dict[str, int] = {}
    for t in content:
        parts = [p for p in t.split("_") if p] or [t]
        counts = []
        for part in parts:
            row = conn.execute("SELECT doc FROM main.chunks_fts_vocab WHERE term = ?",
                               (part,)).fetchone()
            counts.append(row[0] if row else 0)
        df[t] = min(counts)
    present = [t for t in content if df[t] > 0]
    return sorted(present, key=lambda t: (df[t], t))[:k] or content[:k]


def test_ascii_queries_ask_for_exactly_what_they_did(db: IndexDB) -> None:
    """A query with no letter or digit outside ASCII gives the same terms, so the
    same MATCH and the same results, as before H12.3: punctuation, quotes and
    symbols outside ASCII included."""
    queries = [
        'claude -p "120s" (syntax) e.g. it\'s', "smriti_wake hook", "C++ and C#", "foo.bar()",
        "#tag @user $HOME ~/x", "a*b NEAR(x y) AND OR NOT", "don't won't can't", "e-mail re:x",
        "“curly” ‘quotes’ — dash … ellipsis", "100% 3.14 v0.2.0",
        "x_y_z __init__ _private", "what is this", "Sam's nightly digest, with Ari",
        "the memory nightly digest rollup index and also the witness timestamp anchor "
        "for the zebrafish and quokka habitat notes today",
    ]
    rng = random.Random(1203)
    alphabet = "abcdeAB01_ -.,:;'\"()*^+{}[]#@!?/\\|~`$%&=<>—“”"
    queries += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60)))
                for _ in range(400)]
    words = ["nightly", "digest", "lantern", "door", "notes", "rollups", "sam", "ari"]
    queries += [" ".join(rng.choice(words + [f"w{i}" for i in range(30)])
                         for _ in range(rng.randint(10, 25))) for _ in range(100)]
    for q in queries:
        before = fts_terms_before_h12_3(db.conn, q)
        assert fts_terms(db, q) == before, q
        assert fts_match(fts_terms(db, q)) == " OR ".join(f'"{t}"' for t in before), q

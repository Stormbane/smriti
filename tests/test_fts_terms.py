"""fts_terms keeps long queries cheap: dedupe, drop stopwords, keep the rarest terms."""
from __future__ import annotations

import sqlite3

import pytest

from smriti.store.search import fts_terms


@pytest.fixture()
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, source)")
    except sqlite3.OperationalError:
        pytest.skip("FTS5 not available")
    common = "memory nightly digest rollup index"
    rows = [f"{common} chunk {i}" for i in range(40)]
    rows += ["witness timestamp anchor", "zebrafish", "quokka habitat"]
    db.executemany("INSERT INTO chunks_fts(content, heading, source) VALUES (?, '', '')",
                   [(r,) for r in rows])
    return db


def test_long_query_keeps_only_the_rarest_terms(conn: sqlite3.Connection) -> None:
    q = ("please can you check the memory nightly digest rollup index and also the witness "
         "timestamp anchor for the zebrafish and quokka habitat notes today")
    terms = fts_terms(conn, q, k=4)
    assert len(terms) == 4
    assert set(terms) <= {"zebrafish", "quokka", "habitat", "witness", "timestamp", "anchor"}
    assert "memory" not in terms and "please" not in terms


def test_short_query_is_deduplicated_and_lowercased(conn: sqlite3.Connection) -> None:
    assert fts_terms(conn, "Witness witness ANCHOR") == ["witness", "anchor"]


def test_punctuation_cannot_break_the_fts_parser(conn: sqlite3.Connection) -> None:
    terms = fts_terms(conn, 'claude -p "120s" (syntax) e.g. it\'s')
    assert all(t.isalnum() or "_" in t for t in terms)
    conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
                 (" OR ".join(f'"{t}"' for t in terms),)).fetchall()


def test_all_stopword_query_still_searches(conn: sqlite3.Connection) -> None:
    assert fts_terms(conn, "what is this") == ["what", "is", "this"]


def test_terms_absent_from_the_index_are_dropped_when_others_match(
        conn: sqlite3.Connection) -> None:
    q = " ".join(f"unseenword{i}" for i in range(20)) + " zebrafish"
    assert fts_terms(conn, q, k=12) == ["zebrafish"]


def test_query_only_reader_uses_the_main_schema_vocab(conn: sqlite3.Connection) -> None:
    # MCP and recall open the index with PRAGMA query_only=ON: no temp tables allowed.
    conn.execute("CREATE VIRTUAL TABLE chunks_fts_vocab USING fts5vocab(chunks_fts, 'row')")
    conn.execute("PRAGMA query_only=ON")
    q = ("memory nightly digest rollup index chunk " * 3) + "witness anchor zebrafish quokka"
    q += " " + " ".join(f"filler{i}" for i in range(10))
    assert set(fts_terms(conn, q, k=3)) <= {"zebrafish", "quokka", "witness", "anchor"}


def test_query_only_reader_without_vocab_still_answers(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA query_only=ON")
    q = " ".join(f"term{i:02d}" for i in range(20))
    assert fts_terms(conn, q, k=3) == ["term00", "term01", "term02"]


def test_underscore_identifiers_are_kept_when_their_parts_are_indexed(
        conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO chunks_fts(content, heading, source) VALUES "
                 "('the smriti_wake hook fired', '', '')")
    q = "smriti_wake " + " ".join(f"memory{i}" for i in range(15)) + " zebrafish"
    terms = fts_terms(conn, q, k=12)
    assert "smriti_wake" in terms
    hits = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                        (" OR ".join(f'"{t}"' for t in terms),)).fetchone()[0]
    assert hits >= 1


def test_without_fts5vocab_falls_back_to_the_first_terms() -> None:
    class NoVocab:
        def execute(self, *a: object) -> object:
            raise sqlite3.OperationalError("no such module: fts5vocab")

    q = " ".join(f"term{i:02d}" for i in range(20))
    assert fts_terms(NoVocab(), q, k=3) == ["term00", "term01", "term02"]

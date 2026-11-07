"""H13.12: what a calibrated model's similarities say about a search.

Search had no notion of "later": a note that corrects an earlier one ranked
wherever its words put it, often under what it corrects. And every leg's
scores are rescaled among the candidates found, so the best result scored
high whatever the query, and no irrelevant query fell under a threshold.

For a model with a calibration (``search.CALIBRATIONS``, measured on the
eval set), a later version of a note takes the place of the earlier one it
restates, and a search whose best vector hit isn't close to the query says
so, its scores scaled down. A model without one is searched as before.
These tests use the stub embedder (word hashes) with a calibration made for
it; the real models' numbers are in the eval (``smriti eval``) and the
needs_model test in test_eval_quality.py.
"""

from __future__ import annotations

import importlib
import sqlite3
import struct
from pathlib import Path

import pytest

from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.schema import IndexDB, open_readonly
from smriti.store.search import Calibration, SearchOutcome, chunk_date, search_with_status
from tests._vec import needs_vec

# The module (smriti.store re-exports a function of the same name).
search_mod = importlib.import_module("smriti.store.search")


@pytest.mark.parametrize(("source", "content", "day"), [
    ("journal/2026/05/week2/05-11.md", "# Drawer check\n\nNo date here.", "2026-05-11"),
    ("projects/kite/2026/05-13.md", "2026-01-01: an older date in the text", "2026-05-13"),
    ("log/2026/05/13-digest.md", "# Day digest", "2026-05-13"),
    ("notes/2026-05-03-paste.md", "", "2026-05-03"),
    ("notes/glue.md", "# Hinge glue\n\n2026-05-11: Hinge glue dries in an hour.", "2026-05-11"),
    ("notes/glue.md", "---\ndate: 2026-05-12\n---\n# Glue", "2026-05-12"),
    ("notes/supplies.md", "# Supplies\n\nThe orange drawer holds spare awls.", None),
    ("notes/2026/notes.md", "version 12026-05-111 is not a date", None),
    ("projects/kite/2026/13-45.md", "", None),  # no month 13
])
def test_the_day_a_chunk_speaks_for(source: str, content: str, day: str | None) -> None:
    assert chunk_date(source, content) == day


# ── The later version first ──────────────────────────────────────────


def _vector(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


@pytest.fixture()
def vec_conn() -> IndexDB:
    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return IndexDB(conn=conn, has_vec=True, has_fts=False)


def _chunk(source: str, text: str = "") -> search_mod._Candidate:
    return {"source": source, "heading": "", "content": text, "trunk_distance": 1,
            "chunk_id": source}


OLD = _chunk("projects/kite/2026/05-05.md")
OTHER = _chunk("notes/class.md", "2026-05-10: the class")
NEW = _chunk("projects/kite/2026/05-13.md")
NEWEST = _chunk("projects/kite/2026/05-20.md")
UNDATED = _chunk("notes/supplies.md")
CLOSE, FAR = _vector(1.0, 0.0), _vector(0.0, 1.0)


def _reorder(conn: IndexDB, ranked: list[search_mod._Ranked],
             vectors: dict[int, bytes]) -> list[tuple[str, float]]:
    out = search_mod._newer_versions_first(conn, ranked, vectors, same_note=0.75, depth=3)
    return [(cand["source"], score) for _, score, cand in out]


@needs_vec
def test_a_later_version_takes_the_earlier_ones_place_and_score(vec_conn: IndexDB) -> None:
    ranked = [(1, 0.9, OLD), (2, 0.7, OTHER), (3, 0.6, NEW)]
    vectors = {1: CLOSE, 2: FAR, 3: _vector(0.95, 0.1)}
    assert _reorder(vec_conn, ranked, vectors) == [
        (NEW["source"], 0.9), (OTHER["source"], 0.7), (OLD["source"], 0.6)]


@needs_vec
def test_the_newest_of_several_versions_comes_first(vec_conn: IndexDB) -> None:
    ranked = [(1, 0.9, OLD), (3, 0.8, NEW), (4, 0.7, NEWEST)]
    vectors = {1: CLOSE, 3: CLOSE, 4: CLOSE}
    assert [s for s, _ in _reorder(vec_conn, ranked, vectors)] == [
        NEWEST["source"], NEW["source"], OLD["source"]]


@needs_vec
@pytest.mark.parametrize(("ranked", "vectors"), [
    # Not versions of one note: not similar enough.
    ([(1, 0.9, OLD), (3, 0.8, NEW)], {1: CLOSE, 3: _vector(0.6, 0.8)}),
    # A clearly worse match: under half the place's score.
    ([(1, 0.9, OLD), (3, 0.4, NEW)], {1: CLOSE, 3: CLOSE}),
    # Already in order, or not dated.
    ([(3, 0.9, NEW), (1, 0.8, OLD)], {1: CLOSE, 3: CLOSE}),
    ([(5, 0.9, UNDATED), (3, 0.8, NEW)], {5: CLOSE, 3: CLOSE}),
    ([(1, 0.9, OLD), (5, 0.8, UNDATED)], {1: CLOSE, 5: CLOSE}),
    # No stored vector (a chunk only the keyword leg found).
    ([(1, 0.9, OLD), (3, 0.8, NEW)], {1: CLOSE}),
], ids=["dissimilar", "worse-match", "in-order", "undated-place", "undated-later", "no-vector"])
def test_nothing_else_moves(vec_conn: IndexDB, ranked: list[search_mod._Ranked],
                            vectors: dict[int, bytes]) -> None:
    before = [(cand["source"], score) for _, score, cand in ranked]
    assert _reorder(vec_conn, ranked, vectors) == before


# ── Through search ───────────────────────────────────────────────────

NOTES = {
    # The earlier note shares one more word with the query ("field"), so it
    # ranks first on words alone; the correction restates it.
    "projects/kite/2026/05-05.md":
        "# Kite battery\n\nKite uses a replaceable AA battery pack for field repairs, "
        "Sam decided.\n",
    "projects/kite/2026/05-13.md":
        "# Kite battery\n\nCorrection: Kite uses a replaceable AAA battery pack for repairs, "
        "Sam decided.\n",
    "notes/supplies.md": "# Supplies\n\nThe blue cabinet holds linen thread and brass clips.\n",
}


@pytest.fixture()
def stub_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from smriti.store.indexer import index_tree

    root = tmp_path / "tree"
    for rel, text in NOTES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    stub = StubEmbedding()
    monkeypatch.setattr(embedding, "embedder", lambda: stub)
    db = root / ".smriti" / "index.db"
    index_tree(root=root, db=db)
    return db


def _search(db: Path, query: str) -> SearchOutcome:
    conn = open_readonly(db)
    try:
        return search_with_status(conn, query, use_reranker=False)
    finally:
        conn.close()


def _calibrate(monkeypatch: pytest.MonkeyPatch, unrelated_below: float,
               same_note: float) -> None:
    monkeypatch.setattr(search_mod, "CALIBRATIONS",
                        {"stub-384": Calibration(unrelated_below, same_note)})


@needs_vec
def test_a_search_puts_the_correction_where_the_note_it_corrects_was(
        stub_index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    query = "Kite replaceable battery pack for field repairs"
    before = _search(stub_index, query)
    assert [r.source for r in before.results[:2]] == [
        "projects/kite/2026/05-05.md", "projects/kite/2026/05-13.md"]  # uncalibrated: as before

    _calibrate(monkeypatch, unrelated_below=0.1, same_note=0.75)
    after = _search(stub_index, query)

    assert [r.source for r in after.results[:2]] == [
        "projects/kite/2026/05-13.md", "projects/kite/2026/05-05.md"]
    assert [r.score for r in after.results] == [r.score for r in before.results]
    assert after.unrelated_below is None and not after.notes()


@needs_vec
def test_a_search_with_nothing_close_says_so_and_scores_down(
        stub_index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    query = "linen thread"
    before = _search(stub_index, query)
    assert before.similarity is not None and before.unrelated_below is None

    _calibrate(monkeypatch, unrelated_below=0.99, same_note=1.0)
    weak = _search(stub_index, query)

    assert weak.unrelated_below == 0.99 and weak.similarity == before.similarity
    assert [r.source for r in weak.results] == [r.source for r in before.results]
    assert [r.score for r in weak.results] == pytest.approx(
        [r.score * before.similarity for r in before.results])
    assert weak.results[0].score < 0.45  # under the recall threshold, and any above it
    assert "nothing is close to the query" in weak.notes()[0]

    _calibrate(monkeypatch, unrelated_below=0.01, same_note=1.0)  # close enough
    assert [r.score for r in _search(stub_index, query).results] == [
        r.score for r in before.results]


@needs_vec
def test_keyword_only_is_not_calibrated(stub_index: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyword-only has no similarity to judge by: it abstains only when no
    keyword matches, and nothing is reordered."""
    _calibrate(monkeypatch, unrelated_below=0.99, same_note=0.0)
    conn = open_readonly(stub_index)
    try:
        outcome = search_with_status(conn, "Kite replaceable battery pack for field repairs",
                                     use_reranker=False, use_vectors=False)
        nothing = search_with_status(conn, "Neptune orbital eccentricity",
                                     use_reranker=False, use_vectors=False)
    finally:
        conn.close()
    assert outcome.similarity is None and outcome.unrelated_below is None
    assert outcome.results[0].source == "projects/kite/2026/05-05.md"
    assert nothing.results == []

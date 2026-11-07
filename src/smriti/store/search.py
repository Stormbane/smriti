"""Search the smriti index with hybrid vector + keyword retrieval.

The search flow:
1. Embed the query with the same provider used for indexing; the embedding's
   identity is checked against the index's in the snapshot the vectors are
   read from (otherwise the search is keyword-only, and says why).
2. Vector search via sqlite-vec (top-k by cosine distance).
3. Keyword search via FTS5 (top-k by BM25 rank).
4. Merge and deduplicate both result sets.
5. Score: ``alpha * vec_score + beta * fts_score + gamma * trunk_boost``.
6. Optionally rerank with a cross-encoder.
7. For a model with a :data:`CALIBRATIONS` entry: a later version of a note
   takes the place of the earlier one it restates, and a search whose best
   hit is not close to the query at all says so, its scores scaled down
   (except a keyword match whose vectors are pending, which no similarity
   measured).
8. Return top results.

Every score above is relative to the candidates found (each leg is rescaled
to 0-1 among them), so the best result scores high whatever the query; only
the raw similarity of the best vector hit says whether anything is close.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from smriti.store.schema import IndexDB

if TYPE_CHECKING:
    from smriti.embed.identity import Embedded

log = logging.getLogger(__name__)

# ── Scoring weights ──────────────────────────────────────────────────

VEC_WEIGHT = 0.5
FTS_WEIGHT = 0.3
TRUNK_WEIGHT = 0.2


@dataclass(frozen=True)
class Calibration:
    """What one embedding model's raw cosine similarities mean, measured with it on
    the eval set (``smriti eval``'s quality set, :mod:`smriti.eval.quality`).

    ``unrelated_below``: under it, the best vector hit is not close to the query:
    the irrelevant queries' best hits fall under it, every answerable one's above.
    ``same_note``: two chunks at least this similar restate one note: on the
    eval set every pair above it is a later note restating an earlier one, and
    no other pair is.
    """

    unrelated_below: float
    same_note: float


# Per model: similarities don't compare across models, and a model without an
# entry is searched exactly as before. Measured 2026-11-07 on quality set v1:
# - bge-m3 (onnx, int8; new installs): irrelevant queries' best hits 0.32-0.43,
#   answerable 0.42-0.76, the lowest a paraphrase (0.4206) under two irrelevant
#   ones; 0.40 sits between the next irrelevant (0.387) and it, so four of six
#   irrelevant queries are marked and no answerable one. Restating pairs
#   0.777-0.848; the closest other pair 0.743.
# - all-MiniLM-L6-v2 (local; indexes built before onnx was the default):
#   irrelevant 0.121-0.243, answerable 0.178-0.849; 0.165 sits between 0.151
#   and the lowest answerable (0.178): five of six marked, none answerable.
#   Restating pairs 0.786-0.880; the closest other pair 0.726.
CALIBRATIONS: dict[str, Calibration] = {
    "gpahal/bge-m3-onnx-int8": Calibration(unrelated_below=0.40, same_note=0.75),
    "all-MiniLM-L6-v2": Calibration(unrelated_below=0.165, same_note=0.75),
}
# A later version moves up only when it scores at least this share of the
# place it takes: recency never outranks a clearly better match.
NEWER_MIN_SHARE = 0.5


@dataclass
class SearchResult:
    """A single search result."""

    source: str
    heading: str
    content: str
    score: float
    trunk_distance: int
    chunk_id: str


class _Candidate(TypedDict):
    source: str
    heading: str
    content: str
    trunk_distance: int
    chunk_id: str


@dataclass
class SearchOutcome:
    """The results, and why the search was by keyword only when vectors were wanted."""

    results: list[SearchResult]
    keyword_only: str = ""  # the reason; "" when the vector leg ran (or wasn't asked for)
    pending: int = 0  # sources searchable by keyword only for now: their vectors are pending
    similarity: float | None = None  # the best vector hit's raw cosine similarity to the query
    unrelated_below: float | None = None  # set when ``similarity`` was under the model's mark
    unmeasured: int = 0  # results then kept as scored: found by keyword, their vectors pending

    def notes(self) -> list[str]:
        """What a reader must know when the search was less than hybrid, or found nothing close."""
        if self.keyword_only:
            return [f"keyword-only: {self.keyword_only}"]
        notes = []
        if self.unrelated_below is not None and self.similarity is not None:
            if self.unmeasured:
                some = "1 result" if self.unmeasured == 1 else f"{self.unmeasured} results"
                notes.append(f"note: nothing with vectors is close to the query (the best "
                             f"match's similarity is {self.similarity:.2f}, under "
                             f"{self.unrelated_below:.2f}), and those results are scored "
                             f"down; {some} found by keyword, vectors pending, keep their "
                             "scores")
            else:
                notes.append(f"note: nothing is close to the query (the best match's "
                             f"similarity is {self.similarity:.2f}, under "
                             f"{self.unrelated_below:.2f}); these results are likely "
                             "unrelated, and scored down")
        if self.pending:
            some = "1 source is" if self.pending == 1 else f"{self.pending} sources are"
            notes.append(f"note: {some} searchable by keyword only for now (vectors pending)")
        return notes


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _trunk_boost(distance: int) -> float:
    """Boost score for files closer to the trunk."""
    if distance < 0:
        return 0.0
    return 1.0 / (1.0 + distance)


def _normalize(scores: list[float]) -> list[float]:
    """Normalize scores to [0, 1]."""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi == lo:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
# journal/2026/05/week2/05-11.md, projects/kite/2026/05-13.md (the writer's paths)
_PATH_MONTH_DAY = re.compile(
    r"(?:^|/)(\d{4})/(?:\d{2}/)?(?:week\d+/)?(\d{2})-(\d{2})(?:[^/\d][^/]*)?$")
# log/2026/05/13-digest.md (the day-log's)
_PATH_DAY = re.compile(r"(?:^|/)(\d{4})/(\d{2})/(\d{2})(?:[^/\d][^/]*)?$")


def chunk_date(source: str, content: str) -> str | None:
    """The day a chunk speaks for, ``YYYY-MM-DD``, or None when it gives none.

    From its file's path when the path is dated (the writer's daily files, the
    day-log's digests, or an ISO date in a name), else from the first ISO date
    in its text (a frontmatter ``date:``, a dated line). A living note with no
    date anywhere has none.
    """
    for pattern in (_ISO_DATE, _PATH_MONTH_DAY, _PATH_DAY):
        found = pattern.search(source)
        if found and (day := _day(*found.groups())):
            return day
    found = _ISO_DATE.search(content)
    return _day(*found.groups()) if found else None


def _day(year: str, month: str, day: str) -> str | None:
    if not ("01" <= month <= "12" and "01" <= day <= "31"):
        return None
    return f"{year}-{month}-{day}"


FTS_MAX_TERMS = 12

_STOPWORDS = frozenset("""
a an the and or but if then else of to in on at by for with from into onto is are was were be
been being do does did have has had i you he she it we they me my your our their this that
these those there here what which who whom how why when where can could would should will shall
may might must not no yes so just also very please let lets ok okay im its dont youre about as
up out over all any some more most than too now again get got make made want need like use using
""".split())


def fts_terms(conn: IndexDB | sqlite3.Connection, query: str, k: int = FTS_MAX_TERMS) -> list[str]:
    """The query's keyword terms for FTS5: deduplicated, stopwords dropped, rarest *k* kept.

    ORing every word of a long prompt made FTS5 rank most of the index: p90
    13 s on real prompts, against 31 ms with the 12 rarest terms (retrieval
    eval, 2026-11-04). Rarity comes from a temp-schema fts5vocab table, so a
    read-only connection works and index.db is never modified. Each term is
    matched as a quoted phrase (:func:`fts_match`); see :func:`_query_word`
    for what a word becomes.
    """
    raw = [t for t in (_query_word(w) for w in query.strip().split()) if t]
    seen: set[str] = set()
    content: list[str] = []
    for t in raw:
        tl = t.lower()
        # A word in another script is kept however short: 記憶 is two characters.
        if (len(tl) >= 3 or not tl.isascii()) and tl not in _STOPWORDS and tl not in seen:
            seen.add(tl)
            content.append(tl)
    if not content:  # all stopwords or short tokens: still search, just bounded
        return list(dict.fromkeys(t.lower() for t in raw))[:k]
    if len(content) <= k:
        return content
    vocab = _vocab_table(conn)
    if vocab is None:
        return content[:k]
    # unicode61 splits on '_', so `smriti_wake` is indexed as `smriti` + `wake`
    # and matched as their phrase: its frequency is at most its rarest part's.
    # A word in another script is split and folded by the tokenizer itself.
    parts = {t: [p for p in t.split("_") if p] or [t] for t in content if t.isascii()}
    wide = [t for t in content if not t.isascii()]
    if wide:
        tokens = _tokenized(wide)
        if tokens is None:
            return content[:k]
        parts.update(tokens)
    df: dict[str, int] = {}
    try:
        for t in content:
            counts = []
            for part in parts[t]:
                row = conn.execute(
                    f"SELECT doc FROM {vocab} WHERE term = ?", (part,)
                ).fetchone()
                counts.append(row[0] if row else 0)
            df[t] = min(counts, default=0)
    except sqlite3.Error:
        return content[:k]
    present = [t for t in content if df[t] > 0]  # absent terms can't match anything
    return sorted(present, key=lambda t: (df[t], t))[:k] or content[:k]


_ASCII_NONWORD = re.compile(r"[^A-Za-z0-9_]")


def _query_word(word: str) -> str:
    """What one whitespace-separated word of a query is searched as ("" for nothing).

    A word with a letter or digit outside ASCII (``café``, ``स्मृति``, ``記憶``)
    is kept whole, less the punctuation at its ends: quoted, it reaches FTS5's
    unicode61 tokenizer, which splits and folds it as it did the indexed text
    (``café`` is the token ``cafe``). Stripped to ``[A-Za-z0-9_]``, as every word
    was before H12.3, ``café`` became ``caf`` and a Devanagari or CJK word
    nothing. Any other word is stripped to those characters as before, so an
    ASCII query asks for exactly what it always did: ``e.g.`` is ``eg``.
    """
    if word.isascii() or not any(c.isalnum() and not c.isascii() for c in word):
        return _ASCII_NONWORD.sub("", word)
    start, end = 0, len(word)
    while start < end and not _word_char(word[start]):
        start += 1
    while end > start and not _word_char(word[end - 1]):
        end -= 1
    return word[start:end]


def _word_char(c: str) -> bool:
    """A letter, digit, ``_`` or combining mark (a Devanagari vowel sign is a mark)."""
    import unicodedata

    return c.isalnum() or c == "_" or unicodedata.category(c).startswith("M")


def _tokenized(terms: list[str]) -> dict[str, list[str]] | None:
    """Each term's tokens, as FTS5's tokenizer makes them, or None if it can't be asked.

    Read from a private in-memory FTS5 table with chunks_fts's tokenizer (the
    default unicode61, schema.py), so the vocabulary lookup uses SQLite's own
    folding and splitting: ``CAFÉ`` is ``cafe``, and ``स्मृति`` is three
    tokens, its vowel signs and virama being separators to unicode61.
    """
    try:
        mem = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return None
    try:
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        mem.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        mem.executemany("INSERT INTO t(rowid, x) VALUES (?, ?)", list(enumerate(terms, 1)))
        tokens: dict[int, list[str]] = {}
        for doc, term in mem.execute("SELECT doc, term FROM v"):
            tokens.setdefault(doc, []).append(term)
        return {t: tokens.get(i, []) for i, t in enumerate(terms, 1)}
    except sqlite3.Error:
        return None
    finally:
        mem.close()


def fts_match(terms: list[str]) -> str:
    """The FTS5 MATCH expression for ``terms``: each a quoted phrase, ORed.

    Quoted, a term is only text for the tokenizer, never syntax: ``AND``,
    ``NEAR``, ``*`` and ``:`` mean nothing inside, and a ``"`` is doubled.
    """
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _vocab_table(conn: IndexDB | sqlite3.Connection) -> str | None:
    """The fts5vocab table to read term frequencies from, or None.

    The index schema creates ``chunks_fts_vocab`` in main, which query_only
    readers (MCP, recall) can read but not create. Indexes built before it
    existed get a temp one on writable connections.
    """
    try:
        conn.execute("SELECT 1 FROM main.chunks_fts_vocab LIMIT 1")
        return "main.chunks_fts_vocab"
    except sqlite3.Error:
        pass
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.chunks_fts_vocab "
            "USING fts5vocab(main, chunks_fts, 'row')"
        )
        return "temp.chunks_fts_vocab"
    except sqlite3.Error:
        return None


@contextmanager
def _snapshot(conn: IndexDB) -> Iterator[None]:
    """One read transaction: every read inside sees the same state of the index."""
    raw = getattr(conn, "conn", None)
    if not isinstance(raw, sqlite3.Connection) or raw.in_transaction:
        yield
        return
    raw.execute("BEGIN")
    try:
        yield
    finally:
        raw.commit()


def _fetch_candidates(conn: IndexDB, rowids: list[int]) -> dict[int, _Candidate]:
    """The chunks at ``rowids``, by rowid (one statement; a rowid with no chunk is left out)."""
    if not rowids:
        return {}
    marks = ",".join("?" * len(rowids))
    rows = conn.execute(
        "SELECT rowid, id, source, heading, content, trunk_distance FROM chunks "
        f"WHERE rowid IN ({marks})", rowids).fetchall()
    return {row[0]: _candidate(row[1:]) for row in rows}


def _candidate(row: Sequence[Any]) -> _Candidate:
    chunk_id, source, heading, content, distance = row
    return {"chunk_id": chunk_id, "source": source, "heading": heading, "content": content,
            "trunk_distance": distance}


@dataclass
class _Hits:
    """What the vector leg found besides its scores."""

    why_not: str = ""  # why it found nothing, or ""
    model: str = ""  # the model the query was embedded with
    best: float | None = None  # the best hit's raw cosine similarity to the query
    rowids: set[int] = field(default_factory=set)  # the hits


def _vector_leg(conn: IndexDB, query: str, embedded: Embedded, candidate_k: int,
                scores: dict[int, float], candidates: dict[int, _Candidate]) -> _Hits:
    """Fill ``scores`` and ``candidates`` from the vectors; say why not, or what else it saw.

    Runs inside the search's snapshot. The query's embedding, made before
    it, carries the identity that made it, and it is compared with the
    identity the index records in the snapshot the vectors are then read
    from: vectors of two models are never compared. A mismatch is stale
    work (the model loaded here, or the service, came before a migration):
    the query is embedded once more, afresh.
    """
    from smriti.embed import provider as embedding
    from smriti.embed.identity import recorded_identity, vec_table_dimension

    mismatch = ""
    for attempt in range(2):
        if attempt:
            embedding.forget_provider()
            embedded = embedding.embed(embedding.embedder(), [query], "query")
        recorded = recorded_identity(conn)
        if recorded is not None and not recorded.compatible(embedded.identity):
            mismatch = (f"the query was embedded with {embedded.identity.describe()}, "
                        f"the index with {recorded.describe()}")
            continue
        if vec_table_dimension(conn) is None:
            return _Hits("the index has no vectors yet")
        rows = conn.execute(
            """SELECT rowid, distance
               FROM chunks_vec
               WHERE embedding MATCH ?
               AND k = ?""",
            (_serialize_f32(embedded.vectors[0]), candidate_k),
        ).fetchall()
        normed = _normalize([1.0 - r[1] for r in rows])  # lower distance = higher
        found = _fetch_candidates(conn, [r[0] for r in rows])
        hits = _Hits(model=embedded.identity.model, rowids={r[0] for r in rows})
        for (rowid, _), ns in zip(rows, normed, strict=True):
            scores[rowid] = ns
            if rowid in found:
                candidates[rowid] = found[rowid]  # in the order found: it breaks ties
        if rows:
            # The distance is Euclidean; between unit vectors (every provider
            # here makes them) the cosine is 1 - d^2 / 2.
            hits.best = 1.0 - rows[0][1] ** 2 / 2.0
        return hits
    embedding.forget_provider()
    return _Hits(f"embedding identity mismatch: {mismatch}")


_Ranked = tuple[int, float, _Candidate]  # rowid, score, chunk


def _newer_versions_first(conn: IndexDB, ranked: list[_Ranked], hits: set[int],
                          same_note: float) -> list[_Ranked]:
    """Among ``ranked``, each place goes to the newest later version of its chunk.

    A later version: a chunk ranked below it, dated after it
    (:func:`chunk_date`), scoring at least :data:`NEWER_MIN_SHARE` of the
    place's score, and at least ``same_note`` similar by their stored
    vectors (both among the vector ``hits``). It takes the place and its
    score; the earlier chunk takes the later one's place and score. Nothing
    else moves, so recency only reorders versions of one note; an undated
    chunk never moves for it. A stored vector is read only for a pair the
    dates and scores allow (each read costs about half a millisecond).
    """
    order = list(ranked)
    dates: dict[int, str | None] = {}
    stored: dict[int, bytes | None] = {}

    def date_of(rowid: int, cand: _Candidate) -> str | None:
        if rowid not in dates:
            dates[rowid] = chunk_date(cand["source"], cand["content"])
        return dates[rowid]

    def vector(rowid: int) -> bytes | None:
        if rowid not in stored:
            row = conn.execute("SELECT embedding FROM chunks_vec WHERE rowid = ?",
                               (rowid,)).fetchone()
            stored[rowid] = row[0] if row else None
        return stored[rowid]

    for i in range(len(order)):
        while True:
            rowid, score, cand = order[i]
            date = date_of(rowid, cand)
            if date is None or rowid not in hits:
                break
            later = [(day, j) for j in range(i + 1, len(order))
                     if order[j][1] >= NEWER_MIN_SHARE * score and order[j][0] in hits
                     and (day := date_of(order[j][0], order[j][2])) is not None and day > date]
            newest = next((j for _, j in sorted(later, reverse=True)
                           if _restates(conn, vector(rowid), vector(order[j][0]), same_note)),
                          None)
            if newest is None:
                break
            (r_old, s_old, c_old), (r_new, s_new, c_new) = order[i], order[newest]
            order[i], order[newest] = (r_new, s_old, c_new), (r_old, s_new, c_old)
    return order


def _scored_down(ranked: list[_Ranked], keep: set[int], factor: float) -> list[_Ranked]:
    """``ranked`` with every score but ``keep``'s multiplied by ``factor``, sorted again.

    The search's best vector hit isn't close to the query: each score it
    speaks for falls under the model's mark (and any threshold above it),
    in the same order (a stable sort). ``keep``: chunks found by keyword
    whose vectors are pending, which no similarity measured: a write
    without vectors stays searchable by keyword, so they keep their scores.
    """
    return sorted(((rowid, score if rowid in keep else score * factor, cand)
                   for rowid, score, cand in ranked), key=lambda x: x[1], reverse=True)


def _pending_rowids(conn: IndexDB, ranked: list[_Ranked], hits: set[int]) -> set[int]:
    """The ranked chunks no similarity measured: not vector hits, their source's vectors pending.

    Only the keyword leg can find such a chunk. A chunk of a source with
    vectors that isn't a hit is measured all the same: it is less similar
    than the last hit.
    """
    sources = sorted({cand["source"] for rowid, _, cand in ranked if rowid not in hits})
    if not sources:
        return set()
    marks = ",".join("?" * len(sources))
    pending = {row[0] for row in conn.execute(
        f"SELECT source FROM sources WHERE vectors = 0 AND source IN ({marks})", sources)}
    return {rowid for rowid, _, cand in ranked if rowid not in hits and cand["source"] in pending}


def _restates(conn: IndexDB, a: bytes | None, b: bytes | None, same_note: float) -> bool:
    """Whether two stored vectors are at least ``same_note`` similar (sqlite-vec's cosine)."""
    if a is None or b is None:
        return False
    distance = conn.execute("SELECT vec_distance_cosine(?, ?)", (a, b)).fetchone()[0]
    return 1.0 - float(distance) >= same_note


def _keyword_leg(conn: IndexDB, query: str, candidate_k: int,
                 scores: dict[int, float], candidates: dict[int, _Candidate]) -> None:
    """Fill ``scores``, and ``candidates`` with the chunks only it found, from FTS5.

    Inside the search's snapshot, so the chunks read are the ones matched: a
    writer replacing chunks meanwhile (SQLite reuses their rowids) can't
    pair one chunk's rank with another's text.
    """
    # Each term a quoted phrase, ORed for broad recall: quoted, no
    # query ("claude -p 120s", `e.g.`, `it's`, NEAR) can break the parser.
    fts_query = fts_match(fts_terms(conn, query))
    if not fts_query:
        return
    rows = conn.execute(
        """SELECT rowid, rank
           FROM chunks_fts
           WHERE chunks_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (fts_query, candidate_k),
    ).fetchall()
    # FTS5 rank is negative (lower = better), flip for normalization
    normed = _normalize([-r[1] for r in rows])
    found = _fetch_candidates(conn, [r[0] for r in rows if r[0] not in candidates])
    for (rowid, _), ns in zip(rows, normed, strict=True):
        scores[rowid] = ns
        if rowid in found:
            candidates[rowid] = found[rowid]


def search(
    conn: IndexDB,
    query: str,
    *,
    top_k: int = 5,
    candidate_k: int = 20,
    use_reranker: bool = True,
    use_vectors: bool = True,
) -> list[SearchResult]:
    """Search the index for chunks matching *query* (see :func:`search_with_status`)."""
    return search_with_status(conn, query, top_k=top_k, candidate_k=candidate_k,
                              use_reranker=use_reranker, use_vectors=use_vectors).results


def search_with_status(
    conn: IndexDB,
    query: str,
    *,
    top_k: int = 5,
    candidate_k: int = 20,
    use_reranker: bool = True,
    use_vectors: bool = True,
) -> SearchOutcome:
    """Search the index for chunks matching *query*, saying when it was keyword-only.

    Parameters
    ----------
    conn:
        Database connection (from ``ensure_schema``).
    query:
        Natural language query string.
    top_k:
        Number of results to return.
    candidate_k:
        Number of candidates to retrieve from each search method before
        merging.
    use_reranker:
        If True and a cross-encoder is available, rerank merged candidates.
    use_vectors:
        If False, skip the vector leg (keyword search only). Short-lived
        processes such as hooks use this: loading the embedding model
        costs 5-18 s per process.
    """
    import time as _time
    t0 = _time.monotonic()

    candidates: dict[int, _Candidate] = {}  # rowid → {source, heading, content, ...}
    has_vec = conn.has_vec and use_vectors
    keyword_only = "" if has_vec or not use_vectors else "sqlite-vec is not available"
    vec_scores: dict[int, float] = {}
    fts_scores: dict[int, float] = {}
    hits = _Hits()
    pending = 0

    # The query is embedded before the snapshot opens: a read transaction
    # held while the embedder answers would only pin the WAL.
    embedded: Embedded | None = None
    if has_vec:
        from smriti.embed import provider as embedding

        try:
            embedded = embedding.embed(embedding.embedder(), [query], "query")
        except Exception as exc:  # noqa: BLE001 — logged and reported; falls back to keywords
            log.warning("Vector search failed; searching by keyword only: %s", exc)
            keyword_only = embedding.failure_reason(exc)

    # Every read of the index, both legs and the stored vectors the
    # adjustments compare, in one snapshot: a writer that replaces chunks
    # meanwhile can't pair one chunk's score with another's text, nor the two
    # legs' scores of a rowid with two chunks. WAL lets writers commit
    # meanwhile; this search just doesn't see them. (The CLI's reranker runs
    # inside it too: a read transaction held for it holds up no writer.)
    with _snapshot(conn):
        if has_vec:
            from smriti.embed.provider import failure_reason
            from smriti.store.schema import pending_vectors

            pending = pending_vectors(conn)
            if embedded is not None:
                try:
                    hits = _vector_leg(conn, query, embedded, candidate_k,
                                       vec_scores, candidates)
                    keyword_only = hits.why_not
                except Exception as exc:  # noqa: BLE001 — logged and reported; keywords instead
                    log.warning("Vector search failed; searching by keyword only: %s", exc)
                    keyword_only = failure_reason(exc)
            if keyword_only:
                # Fall back to exactly a keyword-only search: without this the
                # vector weight stayed in the sum with nothing behind it, and
                # every score silently lost half its range.
                has_vec = False
                hits = _Hits()
                vec_scores.clear()
                candidates.clear()

        if conn.has_fts:
            try:
                _keyword_leg(conn, query, candidate_k, fts_scores, candidates)
            except sqlite3.Error as exc:
                log.warning("FTS search failed: %s", exc)

        if not candidates:
            return SearchOutcome([], keyword_only, pending, hits.best)

        # ── Combined scoring ─────────────────────────────────────────
        scored: list[_Ranked] = []
        # Without a vector leg, keyword relevance takes the vector weight too,
        # so scores stay on the same 0-1 scale and callers' thresholds still
        # mean the same thing (otherwise nothing below the root could exceed 0.4).
        fts_weight = FTS_WEIGHT if has_vec else FTS_WEIGHT + VEC_WEIGHT
        for rowid, cand in candidates.items():
            vs = vec_scores.get(rowid, 0.0)
            fs = fts_scores.get(rowid, 0.0)
            tb = _trunk_boost(cand["trunk_distance"])
            combined = VEC_WEIGHT * vs + fts_weight * fs + TRUNK_WEIGHT * tb
            scored.append((rowid, combined, cand))

        scored.sort(key=lambda x: x[1], reverse=True)

        # ── Is anything close? (a calibrated model only) ─────────────
        # Decided on every candidate, before any cut to top_k: scoring the
        # unrelated down reorders them, and a keyword match whose vectors are
        # pending (no similarity measured it) may then come first.
        calibration = CALIBRATIONS.get(hits.model) if has_vec else None
        unrelated_below: float | None = None
        unmeasured: set[int] = set()
        factor = 1.0
        if (calibration is not None and hits.best is not None
                and hits.best < calibration.unrelated_below):
            unrelated_below = calibration.unrelated_below
            unmeasured = _pending_rowids(conn, scored, hits.rowids) if pending else set()
            factor = max(hits.best, 0.0)
            scored = _scored_down(scored, unmeasured, factor)

        # ── Rerank (optional), else the combined score ───────────────
        ranked: list[_Ranked] | None = None
        if use_reranker and len(scored) > 1:
            try:
                from smriti._vendored.memsearch.reranker import rerank

                rerank_input = [
                    {"content": s[2]["content"], "_rowid": s[0], "_cand": s[2]}
                    for s in scored[:candidate_k]
                ]
                # All of them back, cut to top_k below, once scored down.
                ranked = [(item["_rowid"], item.get("score", 0.0), item["_cand"])
                          for item in rerank(query, rerank_input, top_k=len(rerank_input))]
            except Exception as exc:  # noqa: BLE001 — logged; the combined scores stand
                log.warning("Reranker failed; using the combined score: %s", exc)
                ranked = None
        reranked = ranked is not None
        if ranked is None:
            ranked = scored
        elif unrelated_below is not None:
            ranked = _scored_down(ranked, unmeasured, factor)

        # ── The later version first (a calibrated model only) ────────
        # Among the results returned: the scores, and so the cut, are final.
        ranked = ranked[:top_k]
        if calibration is not None:
            ranked = _newer_versions_first(conn, ranked, hits.rowids, calibration.same_note)
    results = [_result(cand, score) for _, score, cand in ranked]

    from smriti.metrics import get_logger, query_fields
    get_logger().log(
        "search_query",
        **query_fields(query),
        vec_candidates=len(vec_scores),
        fts_candidates=len(fts_scores),
        final_results=len(results),
        reranked=reranked,
        keyword_only=bool(keyword_only),
        elapsed_ms=int((_time.monotonic() - t0) * 1000),
        top_score=results[0].score if results else 0,
        top_source=results[0].source if results else "",
        top_similarity=None if hits.best is None else round(hits.best, 4),
        unrelated=unrelated_below is not None,
    )
    return SearchOutcome(results, keyword_only, pending, hits.best, unrelated_below,
                         len(unmeasured))


def _result(cand: _Candidate, score: float) -> SearchResult:
    return SearchResult(source=cand["source"], heading=cand["heading"],
                        content=cand["content"], score=score,
                        trunk_distance=cand["trunk_distance"], chunk_id=cand["chunk_id"])

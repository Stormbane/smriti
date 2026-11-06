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
7. Return top results.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypedDict

from smriti.store.schema import IndexDB

log = logging.getLogger(__name__)

# ── Scoring weights ──────────────────────────────────────────────────

VEC_WEIGHT = 0.5
FTS_WEIGHT = 0.3
TRUNK_WEIGHT = 0.2


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

    def notes(self) -> list[str]:
        """What a reader must know when the search was less than hybrid."""
        if self.keyword_only:
            return [f"keyword-only: {self.keyword_only}"]
        if self.pending:
            some = "1 source is" if self.pending == 1 else f"{self.pending} sources are"
            return [f"note: {some} searchable by keyword only for now (vectors pending)"]
        return []


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


def _fetch_candidate(conn: IndexDB, rowid: int) -> _Candidate | None:
    row = conn.execute(
        "SELECT id, source, heading, content, trunk_distance FROM chunks WHERE rowid = ?",
        (rowid,),
    ).fetchone()
    if not row:
        return None
    return {"chunk_id": row[0], "source": row[1], "heading": row[2], "content": row[3],
            "trunk_distance": row[4]}


def _vector_leg(conn: IndexDB, query: str, candidate_k: int,
                scores: dict[int, float], candidates: dict[int, _Candidate]) -> str:
    """Fill ``scores`` and ``candidates`` from the vectors; return why not, or "".

    The query's embedding carries the identity that made it, and it is
    compared with the identity the index records in the same snapshot the
    vectors are then read from: vectors of two models are never compared.
    A mismatch is stale work (the model loaded here, or the service, came
    before a migration): it is tried once more on a fresh embedding.
    """
    from smriti.embed import provider as embedding
    from smriti.embed.identity import recorded_identity, vec_table_dimension

    mismatch = ""
    for _attempt in range(2):
        embedded = embedding.embed(embedding.embedder(), [query], "query")
        with _snapshot(conn):
            recorded = recorded_identity(conn)
            if recorded is not None and not recorded.compatible(embedded.identity):
                mismatch = (f"the query was embedded with {embedded.identity.describe()}, "
                            f"the index with {recorded.describe()}")
            elif vec_table_dimension(conn) is None:
                return "the index has no vectors yet"
            else:
                rows = conn.execute(
                    """SELECT rowid, distance
                       FROM chunks_vec
                       WHERE embedding MATCH ?
                       AND k = ?""",
                    (_serialize_f32(embedded.vectors[0]), candidate_k),
                ).fetchall()
                normed = _normalize([1.0 - r[1] for r in rows])  # lower distance = higher
                for (rowid, _), ns in zip(rows, normed, strict=True):
                    scores[rowid] = ns
                    cand = _fetch_candidate(conn, rowid)
                    if cand is not None:
                        candidates[rowid] = cand
                return ""
        embedding.forget_provider()
    return f"embedding identity mismatch: {mismatch}"


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

    # ── Vector search ────────────────────────────────────────────────
    has_vec = conn.has_vec and use_vectors
    keyword_only = "" if has_vec or not use_vectors else "sqlite-vec is not available"
    vec_scores: dict[int, float] = {}

    pending = 0
    if has_vec:
        from smriti.embed.provider import failure_reason
        from smriti.store.schema import pending_vectors

        pending = pending_vectors(conn)
        try:
            keyword_only = _vector_leg(conn, query, candidate_k, vec_scores, candidates)
        except Exception as exc:  # noqa: BLE001 — logged and reported; falls back to keywords
            log.warning("Vector search failed; searching by keyword only: %s", exc)
            keyword_only = failure_reason(exc)
        if keyword_only:
            # Fall back to exactly a keyword-only search: without this the
            # vector weight stayed in the sum with nothing behind it, and
            # every score silently lost half its range.
            has_vec = False
            vec_scores.clear()
            candidates.clear()

    # ── FTS search ───────────────────────────────────────────────────
    has_fts = conn.has_fts
    fts_scores: dict[int, float] = {}

    if has_fts:
        try:
            # Each term a quoted phrase, ORed for broad recall: quoted, no
            # query ("claude -p 120s", `e.g.`, `it's`, NEAR) can break the parser.
            fts_query = fts_match(fts_terms(conn, query))
            if fts_query:
                rows = conn.execute(
                    """SELECT rowid, rank
                       FROM chunks_fts
                       WHERE chunks_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_query, candidate_k),
                ).fetchall()
                if rows:
                    # FTS5 rank is negative (lower = better), flip for normalization
                    raw = [-r[1] for r in rows]
                    normed = _normalize(raw)
                    for (rowid, _), ns in zip(rows, normed, strict=True):
                        fts_scores[rowid] = ns
                        if rowid not in candidates:
                            row = conn.execute(
                                """SELECT id, source, heading, content, trunk_distance
                                   FROM chunks WHERE rowid = ?""",
                                (rowid,),
                            ).fetchone()
                            if row:
                                candidates[rowid] = {
                                    "chunk_id": row[0],
                                    "source": row[1],
                                    "heading": row[2],
                                    "content": row[3],
                                    "trunk_distance": row[4],
                                }
        except sqlite3.Error as exc:
            log.warning("FTS search failed: %s", exc)

    if not candidates:
        return SearchOutcome([], keyword_only, pending)

    # ── Combined scoring ─────────────────────────────────────────────
    scored: list[tuple[int, float, _Candidate]] = []
    # Without a vector leg, keyword relevance takes the vector weight too, so
    # scores stay on the same 0-1 scale and callers' thresholds still mean
    # the same thing (otherwise nothing below the root could exceed 0.4).
    fts_weight = FTS_WEIGHT if has_vec else FTS_WEIGHT + VEC_WEIGHT
    for rowid, cand in candidates.items():
        vs = vec_scores.get(rowid, 0.0)
        fs = fts_scores.get(rowid, 0.0)
        tb = _trunk_boost(cand["trunk_distance"])
        combined = VEC_WEIGHT * vs + fts_weight * fs + TRUNK_WEIGHT * tb
        scored.append((rowid, combined, cand))

    scored.sort(key=lambda x: x[1], reverse=True)

    # ── Rerank (optional) ────────────────────────────────────────────
    if use_reranker and len(scored) > 1:
        try:
            from smriti._vendored.memsearch.reranker import rerank

            rerank_input = [
                {"content": s[2]["content"], "_rowid": s[0], "_cand": s[2]}
                for s in scored[:candidate_k]
            ]
            reranked = rerank(query, rerank_input, top_k=top_k)
            results = []
            for item in reranked:
                cand = item["_cand"]
                results.append(
                    SearchResult(
                        source=cand["source"],
                        heading=cand["heading"],
                        content=cand["content"],
                        score=item.get("score", 0.0),
                        trunk_distance=cand["trunk_distance"],
                        chunk_id=cand["chunk_id"],
                    )
                )
            from smriti.metrics import get_logger, query_fields
            get_logger().log(
                "search_query",
                **query_fields(query),
                vec_candidates=len(vec_scores),
                fts_candidates=len(fts_scores),
                final_results=len(results),
                reranked=True,
                keyword_only=bool(keyword_only),
                elapsed_ms=int((_time.monotonic() - t0) * 1000),
                top_score=results[0].score if results else 0,
                top_source=results[0].source if results else "",
            )
            return SearchOutcome(results[:top_k], keyword_only, pending)
        except Exception as exc:  # noqa: BLE001 — logged; the combined scores stand
            log.warning("Reranker failed; using the combined score: %s", exc)

    # ── Return top-k by combined score ───────────────────────────────
    results = []
    for _, score, cand in scored[:top_k]:
        results.append(
            SearchResult(
                source=cand["source"],
                heading=cand["heading"],
                content=cand["content"],
                score=score,
                trunk_distance=cand["trunk_distance"],
                chunk_id=cand["chunk_id"],
            )
        )
    from smriti.metrics import get_logger, query_fields
    get_logger().log(
        "search_query",
        **query_fields(query),
        vec_candidates=len(vec_scores),
        fts_candidates=len(fts_scores),
        final_results=len(results),
        reranked=False,
        keyword_only=bool(keyword_only),
        elapsed_ms=int((_time.monotonic() - t0) * 1000),
        top_score=results[0].score if results else 0,
        top_source=results[0].source if results else "",
    )
    return SearchOutcome(results, keyword_only, pending)

"""Search the smriti index with hybrid vector + keyword retrieval.

The search flow:
1. Embed the query with the same provider used for indexing.
2. Vector search via sqlite-vec (top-k by cosine distance).
3. Keyword search via FTS5 (top-k by BM25 rank).
4. Merge and deduplicate both result sets.
5. Score: ``alpha * vec_score + beta * fts_score + gamma * trunk_boost``.
6. Optionally rerank with a cross-encoder.
7. Return top results.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
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
    read-only connection works and index.db is never modified. Each token
    loses non-alphanumerics, because FTS5 chokes on ` ' . : ( ) *.
    """
    import re

    raw = [re.sub(r"[^A-Za-z0-9_]", "", t) for t in query.strip().split()]
    raw = [t for t in raw if t]
    seen: set[str] = set()
    content: list[str] = []
    for t in raw:
        tl = t.lower()
        if len(tl) >= 3 and tl not in _STOPWORDS and tl not in seen:
            seen.add(tl)
            content.append(tl)
    if not content:  # all stopwords or short tokens: still search, just bounded
        return list(dict.fromkeys(t.lower() for t in raw))[:k]
    if len(content) <= k:
        return content
    vocab = _vocab_table(conn)
    if vocab is None:
        return content[:k]
    df: dict[str, int] = {}
    try:
        for t in content:
            # unicode61 splits on '_', so `smriti_wake` is indexed as `smriti` + `wake`
            # and matched as their phrase: its frequency is at most its rarest part's.
            parts = [p for p in t.split("_") if p] or [t]
            counts = []
            for part in parts:
                row = conn.execute(
                    f"SELECT doc FROM {vocab} WHERE term = ?", (part,)
                ).fetchone()
                counts.append(row[0] if row else 0)
            df[t] = min(counts)
    except sqlite3.Error:
        return content[:k]
    present = [t for t in content if df[t] > 0]  # absent terms can't match anything
    return sorted(present, key=lambda t: (df[t], t))[:k] or content[:k]


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


def search(
    conn: IndexDB,
    query: str,
    *,
    top_k: int = 5,
    candidate_k: int = 20,
    use_reranker: bool = True,
    use_vectors: bool = True,
) -> list[SearchResult]:
    """Search the index for chunks matching *query*.

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

    from smriti.store.indexer import _get_embedding_provider

    candidates: dict[int, _Candidate] = {}  # rowid → {source, heading, content, ...}

    # ── Vector search ────────────────────────────────────────────────
    has_vec = conn.has_vec and use_vectors
    vec_scores: dict[int, float] = {}

    if has_vec:
        try:
            provider = _get_embedding_provider()
            q_embedding = asyncio.run(provider.embed([query]))[0]
            rows = conn.execute(
                """SELECT rowid, distance
                   FROM chunks_vec
                   WHERE embedding MATCH ?
                   AND k = ?""",
                (_serialize_f32(q_embedding), candidate_k),
            ).fetchall()
            if rows:
                raw = [r[1] for r in rows]
                normed = _normalize([1.0 - d for d in raw])  # lower distance = higher score
                for (rowid, _), ns in zip(rows, normed, strict=True):
                    vec_scores[rowid] = ns
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
        except Exception as exc:  # noqa: BLE001 — logged; falls back to keyword-only
            # Fall back to exactly a keyword-only search: without this the
            # vector weight stayed in the sum with nothing behind it, and
            # every score silently lost half its range.
            log.warning("Vector search failed; searching by keyword only: %s", exc)
            has_vec = False
            vec_scores.clear()
            candidates.clear()

    # ── FTS search ───────────────────────────────────────────────────
    has_fts = conn.has_fts
    fts_scores: dict[int, float] = {}

    if has_fts:
        try:
            # Simple query: wrap each word in quotes for exact matching,
            # join with OR for broad recall.
            #
            # FTS5 chokes on special characters (`, ', ., :, parens, *).
            # We strip every non-alphanumeric char from each token before
            # quoting so a query like "claude -p 120s syntax" or content
            # with `e.g.` / `it's` can't break the parser.
            fts_query = " OR ".join(f'"{t}"' for t in fts_terms(conn, query))
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
        return []

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
                elapsed_ms=int((_time.monotonic() - t0) * 1000),
                top_score=results[0].score if results else 0,
                top_source=results[0].source if results else "",
            )
            return results[:top_k]
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
        elapsed_ms=int((_time.monotonic() - t0) * 1000),
        top_score=results[0].score if results else 0,
        top_source=results[0].source if results else "",
    )
    return results

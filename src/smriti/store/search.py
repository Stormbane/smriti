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
from pathlib import Path

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


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 5,
    candidate_k: int = 20,
    use_reranker: bool = True,
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
    """
    import time as _time
    t0 = _time.monotonic()

    from smriti.store.indexer import _get_embedding_provider

    candidates: dict[int, dict] = {}  # rowid → {source, heading, content, ...}

    # ── Vector search ────────────────────────────────────────────────
    has_vec = conn.has_vec
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
                for (rowid, _), ns in zip(rows, normed):
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
        except Exception as exc:
            log.warning("Vector search failed: %s", exc)

    # ── FTS search ───────────────────────────────────────────────────
    has_fts = conn.has_fts
    fts_scores: dict[int, float] = {}

    if has_fts:
        try:
            # Simple query: wrap each word in quotes for exact matching,
            # join with OR for broad recall.
            terms = query.strip().split()
            fts_query = " OR ".join(f'"{t}"' for t in terms if t)
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
                    for (rowid, _), ns in zip(rows, normed):
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
        except Exception as exc:
            log.warning("FTS search failed: %s", exc)

    if not candidates:
        return []

    # ── Combined scoring ─────────────────────────────────────────────
    scored: list[tuple[int, float, dict]] = []
    for rowid, cand in candidates.items():
        vs = vec_scores.get(rowid, 0.0)
        fs = fts_scores.get(rowid, 0.0)
        tb = _trunk_boost(cand["trunk_distance"])
        combined = VEC_WEIGHT * vs + FTS_WEIGHT * fs + TRUNK_WEIGHT * tb
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
            from smriti.metrics import get_logger
            get_logger().log("search_query", query=query, query_len=len(query), vec_candidates=len(vec_scores), fts_candidates=len(fts_scores), final_results=len(results), reranked=True, elapsed_ms=int((_time.monotonic() - t0) * 1000), top_score=results[0].score if results else 0, top_source=results[0].source if results else "")
            return results[:top_k]
        except Exception as exc:
            log.debug("Reranker not available, using combined score: %s", exc)

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
    from smriti.metrics import get_logger
    get_logger().log("search_query", query=query, query_len=len(query), vec_candidates=len(vec_scores), fts_candidates=len(fts_scores), final_results=len(results), reranked=False, elapsed_ms=int((_time.monotonic() - t0) * 1000), top_score=results[0].score if results else 0, top_source=results[0].source if results else "")
    return results

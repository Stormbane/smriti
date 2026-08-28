"""smriti embedded backend — uses ``store.search.search``.

Fallback backend. Slower than qmd because it loads sentence-transformers
each fire (~14s on Windows cold), but has no external dependencies and
uses smriti's own trunk-distance scoring.
"""

from __future__ import annotations

import sqlite3
import time

from smriti.recall.types import RecallMatch, RecallResponse


def is_available() -> bool:
    try:
        from smriti.core.tree import smriti_db_path  # noqa: F401
        from smriti.store.schema import ensure_schema  # noqa: F401
        from smriti.store.search import search  # noqa: F401
        return True
    except Exception:
        return False


def query(text: str, *, top_k: int, timeout_s: float, rerank: bool) -> RecallResponse:
    t0 = time.monotonic()
    try:
        from smriti.core.tree import smriti_db_path
        from smriti.store.schema import open_readonly
        from smriti.store.search import search
    except Exception as exc:
        return RecallResponse(
            matches=[], elapsed_ms=0, backend="smriti",
            error=f"import_failed: {str(exc)[:160]}",
        )

    db_path = smriti_db_path()
    if not db_path.exists():
        return RecallResponse(
            matches=[], elapsed_ms=0, backend="smriti", error="no_index",
        )

    try:
        # Read-only: recall fires on every file-touching tool across
        # every session — it must never take (or wait on) a write lock.
        conn = open_readonly(db_path)
        try:
            results = search(conn, text, top_k=top_k, use_reranker=rerank)
        finally:
            conn.close()
    except Exception as exc:
        return RecallResponse(
            matches=[], elapsed_ms=int((time.monotonic() - t0) * 1000),
            backend="smriti", error=f"search_failed: {str(exc)[:160]}",
        )

    matches = [
        RecallMatch(
            source=r.source,
            snippet=(r.content or "")[:240],
            score=float(r.score),
        )
        for r in results
    ]
    return RecallResponse(
        matches=matches,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        backend="smriti",
    )

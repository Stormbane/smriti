"""smriti embedded backend: smriti's own index, via ``store.search.search``.

The default backend. Recall runs in short-lived hook processes, where
loading an embedding model costs 5-18 s per fire, so it searches by
keyword only (FTS5, rarest terms; tens of ms) unless
``SMRITI_RECALL_VECTORS=1``. Full hybrid search stays available through
``smriti read`` and the MCP server's ``smriti_read``, whose process is warm.
"""

from __future__ import annotations

import os
import time

from smriti.recall.types import RecallMatch, RecallResponse


def is_available() -> bool:
    try:
        from smriti.core.tree import smriti_db_path  # noqa: F401
        from smriti.store.schema import ensure_schema  # noqa: F401
        from smriti.store.search import search  # noqa: F401
        return True
    except ImportError:
        return False


def query(text: str, *, top_k: int, timeout_s: float, rerank: bool) -> RecallResponse:
    t0 = time.monotonic()
    try:
        from smriti.core.tree import smriti_db_path
        from smriti.store.schema import open_readonly
        from smriti.store.search import search
    except ImportError as exc:
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
            vectors = os.environ.get("SMRITI_RECALL_VECTORS", "") == "1"
            results = search(conn, text, top_k=top_k, use_reranker=rerank and vectors,
                             use_vectors=vectors)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — returned as the response's error; the runner logs it
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

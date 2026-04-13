"""Index the narada tree into the smriti SQLite store.

The indexer scans the tree for markdown files, chunks them, embeds the
chunks, and upserts into the database.  Incremental by default — only
files whose mtime has changed since the last index run are re-processed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

from smriti._vendored.memsearch.chunker import chunk_markdown, compute_chunk_id
from smriti._vendored.memsearch.scanner import scan_paths
from smriti.core.tree import smriti_db_path, tree_root, trunk_distance
from smriti.store.schema import ensure_schema

log = logging.getLogger(__name__)


def _serialize_f32(vec: list[float]) -> bytes:
    """Pack a float list into a little-endian f32 blob for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _get_embedding_provider():  # noqa: ANN202
    """Return the best available embedding provider."""
    from smriti._vendored.memsearch.embeddings import get_provider

    # Try ONNX first (lightweight, offline-first)
    try:
        return get_provider("onnx")
    except Exception:
        pass
    # Fall back to local sentence-transformers
    try:
        return get_provider("local")
    except Exception:
        pass
    raise RuntimeError(
        "No embedding provider available. Install onnxruntime or "
        "sentence-transformers: pip install 'smriti[read]'"
    )


def _indexed_files(conn: sqlite3.Connection) -> dict[str, str]:
    """Return a mapping of source path → max(indexed_at) from the db."""
    rows = conn.execute(
        "SELECT source, MAX(indexed_at) FROM chunks GROUP BY source"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _delete_source(conn: sqlite3.Connection, source: str) -> None:
    """Remove all chunks (and their vec/fts rows) for a given source."""
    # Get rowids before deleting so we can clean up vec0
    rowids = [
        r[0]
        for r in conn.execute(
            "SELECT rowid FROM chunks WHERE source = ?", (source,)
        ).fetchall()
    ]
    if rowids:
        placeholders = ",".join("?" * len(rowids))
        if conn.has_vec:
            conn.execute(
                f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                rowids,
            )
        # FTS triggers handle deletion automatically via AFTER DELETE trigger
        conn.execute(
            f"DELETE FROM chunks WHERE rowid IN ("
            f"SELECT rowid FROM chunks WHERE source = ?)",
            (source,),
        )


def index_tree(
    *,
    full: bool = False,
    root: Path | None = None,
    db: Path | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Index the narada tree.

    Parameters
    ----------
    full:
        If True, drop all existing data and re-index from scratch.
    root:
        Tree root (defaults to ``tree_root()``).
    db:
        Database path (defaults to ``smriti_db_path()``).
    verbose:
        Log progress at INFO level.

    Returns
    -------
    dict with keys: scanned, skipped, indexed, chunks, errors
    """
    import time as _time
    from smriti.metrics import get_logger

    t0 = _time.monotonic()

    if root is None:
        root = tree_root()
    if db is None:
        db = smriti_db_path()

    stats = {"scanned": 0, "skipped": 0, "indexed": 0, "chunks": 0, "errors": 0}

    # ── Get embedding provider and dimension ─────────────────────────
    provider = _get_embedding_provider()
    # Probe for dimension with a dummy embed
    dim = provider.dimension
    log.info("Embedding provider: %s (dimension=%d)", provider.model_name, dim)

    # ── Open / create database ───────────────────────────────────────
    conn = ensure_schema(db, dim)

    if full:
        log.info("Full re-index requested — clearing existing data")
        conn.execute("DELETE FROM chunks")
        if conn.has_vec:
            conn.execute("DELETE FROM chunks_vec")
        if conn.has_fts:
            conn.execute("DELETE FROM chunks_fts")
        conn.commit()

    # ── Scan tree ────────────────────────────────────────────────────
    scanned_files = scan_paths([str(root)])
    # Exclude .smriti/ system directory
    scanned_files = [f for f in scanned_files if ".smriti" not in f.path.parts]
    stats["scanned"] = len(scanned_files)
    log.info("Scanned %d files", len(scanned_files))

    # ── Determine which files need (re-)indexing ─────────────────────
    indexed = _indexed_files(conn)
    to_index: list[tuple[Path, str]] = []

    for sf in scanned_files:
        rel_source = str(sf.path.relative_to(root.resolve())).replace("\\", "/")
        prev = indexed.get(rel_source)
        if prev and not full:
            prev_dt = datetime.fromisoformat(prev)
            file_dt = datetime.fromtimestamp(sf.mtime, tz=timezone.utc)
            if file_dt <= prev_dt:
                stats["skipped"] += 1
                continue
        to_index.append((sf.path, rel_source))

    if not to_index:
        log.info("Nothing to index (all files up to date)")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
            (provider.model_name,),
        )
        conn.commit()
        conn.close()
        return stats

    log.info("Indexing %d files (%d skipped)", len(to_index), stats["skipped"])

    # ── Chunk all files ──────────────────────────────────────────────
    all_chunks = []
    chunk_sources = []

    for fpath, rel_source in to_index:
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception as exc:
            log.warning("Could not read %s: %s", fpath, exc)
            stats["errors"] += 1
            continue

        chunks = chunk_markdown(text, source=rel_source)
        for c in chunks:
            cid = compute_chunk_id(
                c.source, c.start_line, c.end_line, c.content_hash, provider.model_name
            )
            all_chunks.append((cid, c, rel_source, fpath))
        chunk_sources.append(rel_source)

    if not all_chunks:
        log.info("No chunks produced")
        conn.close()
        return stats

    # ── Embed all chunks in one batch ────────────────────────────────
    texts = [c.content for _, c, _, _ in all_chunks]
    log.info("Embedding %d chunks...", len(texts))
    embeddings = asyncio.run(provider.embed(texts))

    # ── Delete old data for re-indexed sources ───────────────────────
    for src in chunk_sources:
        _delete_source(conn, src)
    conn.commit()

    # ── Insert ───────────────────────────────────────────────────────
    now = datetime.now(timezone.utc).isoformat()

    for i, (cid, c, rel_source, fpath) in enumerate(all_chunks):
        dist = trunk_distance(fpath, root)
        conn.execute(
            """INSERT OR REPLACE INTO chunks
               (id, source, heading, heading_level, content,
                start_line, end_line, content_hash, trunk_distance, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                rel_source,
                c.heading,
                c.heading_level,
                c.content,
                c.start_line,
                c.end_line,
                c.content_hash,
                dist,
                now,
            ),
        )
        # Get the rowid for vec0 insertion
        rowid = conn.execute(
            "SELECT rowid FROM chunks WHERE id = ?", (cid,)
        ).fetchone()[0]

        if conn.has_vec:
            conn.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, _serialize_f32(embeddings[i])),
            )

    stats["indexed"] = len(chunk_sources)
    stats["chunks"] = len(all_chunks)

    # ── Update metadata ──────────────────────────────────────────────
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
        (now,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
        (provider.model_name,),
    )

    conn.commit()
    conn.close()

    log.info(
        "Indexed %d files → %d chunks (model=%s)",
        stats["indexed"],
        stats["chunks"],
        provider.model_name,
    )
    metrics = get_logger()
    metrics.log("index_completed", **stats, elapsed_ms=int((_time.monotonic() - t0) * 1000), model=provider.model_name, dimension=dim)
    return stats

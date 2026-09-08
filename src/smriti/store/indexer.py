"""Index the narada tree into the smriti SQLite store.

The indexer scans the tree for markdown files, chunks them, embeds the
chunks, and upserts into the database.  Incremental by default — only
files whose mtime has changed since the last index run are re-processed.
"""

from __future__ import annotations

import asyncio
import functools
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


# Registry keys tried when nothing pins the choice, best-first.
_PROVIDER_AUTO_ORDER = ("onnx", "local")

# Map provider class name → registry key, for recording the selection
# in the index's meta table (the class doesn't know its registry key).
_PROVIDER_KEYS = {"OnnxEmbedding": "onnx", "LocalEmbedding": "local"}


def provider_key(provider: object) -> str | None:
    """Registry key ("onnx"/"local") for a provider instance, if known."""
    return _PROVIDER_KEYS.get(type(provider).__name__)


def _provider_for_model(model: str) -> str | None:
    """Legacy inference: which registry provider serves this model name."""
    name = model.lower()
    if "bge-m3" in name:
        return "onnx"
    if "minilm" in name or "mpnet" in name or "sentence-transformers" in name:
        return "local"
    return None


def _recorded_provider_key(db: Path | None = None) -> str | None:
    """The provider the existing index was built with, from its meta table.

    Every process embedding against an existing index must load the SAME
    provider — a different one at best mismatches the vector dimension
    (vec search silently degrades to FTS) and at worst mixes native
    runtimes: trying onnxruntime first and then importing torch into the
    same process aborted intermittently with 0xC0000409 mid-model-load,
    which the harness surfaced as "Connection closed" (2026-09-06).
    """
    if db is None:
        db = smriti_db_path()
    if not db.exists():
        return None
    try:
        from smriti.store.schema import open_readonly

        conn = open_readonly(db)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'embedding_provider'"
            ).fetchone()
            if row:
                return row[0]
            # Indexes built before the provider was recorded (< e8d98c2)
            # still name their model: infer the provider from it rather
            # than let the auto order pick a different one and strand
            # the vectors behind the dimension guard (review).
            row = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
            return _provider_for_model(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        return None


def _provider_preference() -> tuple[str, ...]:
    """Ordered provider keys to try.

    1. ``SMRITI_EMBED_PROVIDER`` env — explicit override, used for
       deliberate migrations (with ``smriti index --full``).
    2. The provider recorded in the index's meta table — self-consistency
       with the data being searched/updated.
    3. Auto order (onnx first: lighter, offline-first).
    """
    import os

    override = os.environ.get("SMRITI_EMBED_PROVIDER", "").strip().lower()
    if override:
        return (override,)
    recorded = _recorded_provider_key()
    if recorded:
        return (recorded,)
    return _PROVIDER_AUTO_ORDER


@functools.cache
def _get_embedding_provider():  # noqa: ANN202
    """Return the embedding provider for this index (cached per process).

    Building the provider costs 5-18s (model load); embedding a chunk
    costs ~0.02s warm. Without the cache every write paid the build
    cost again — the bulk of smriti_write latency in long-lived MCP
    servers. See ``_provider_preference`` for the selection rules.
    """
    from smriti._vendored.memsearch.embeddings import get_provider

    keys = _provider_preference()
    last_error: Exception | None = None
    for key in keys:
        try:
            return get_provider(key)
        except Exception as exc:
            last_error = exc
            log.warning("embedding provider %r unavailable: %s", key, exc)
    raise RuntimeError(
        f"No embedding provider available (tried {', '.join(keys)}; "
        f"last error: {last_error}). Install onnxruntime or "
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
    try:
        # Dimension guard: chunks_vec is created with a fixed dimension
        # and CREATE IF NOT EXISTS silently keeps the old one, so a
        # provider switch would fail every vector insert mid-index. A
        # full re-index rebuilds the vec table for the new provider;
        # an incremental run must refuse loudly instead of half-writing.
        meta_dim_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'dimension'"
        ).fetchone()
        meta_dim = int(meta_dim_row[0]) if meta_dim_row else dim
        if meta_dim != dim:
            if not full:
                raise RuntimeError(
                    f"index dimension is {meta_dim} but provider "
                    f"{provider.model_name!r} embeds at {dim}; run "
                    "'smriti index --full' to migrate the index"
                )
            if conn.has_vec:
                conn.execute("DROP TABLE chunks_vec")
                conn.execute(
                    "CREATE VIRTUAL TABLE chunks_vec USING vec0"
                    f"(embedding float[{dim}])"
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('dimension', ?)",
                (str(dim),),
            )

        key = provider_key(provider)
        if key:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('embedding_provider', ?)",
                (key,),
            )
        conn.commit()

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
                # Guard against orphaned vec rows: INSERT OR REPLACE on
                # chunks frees a rowid without any trigger cleaning its
                # chunks_vec row (vec0 has no triggers), and crash-era
                # partial commits left such orphans behind. When SQLite
                # recycles one of those rowids the plain INSERT hits
                # "UNIQUE constraint failed on chunks_vec" — which
                # crashed every incremental index from mid-July until
                # 2026-09-06. Delete-then-insert is idempotent.
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
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
        # Keep planner statistics fresh on the (growing) chunks table so
        # the source index actually gets picked. Cheap no-op when stats
        # are already current; runs on the nightly cadence.
        try:
            conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError:
            pass
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
    except BaseException:
        # Never leak an open write transaction from a long-lived
        # process: a mid-index crash with the connection left open
        # wedged every writer fleet-wide (2026-08-28, and again via
        # zombie MCP servers on 2026-09-06). Roll back and close on
        # ANY failure, then re-raise.
        try:
            conn.rollback()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        raise


def index_file(path: Path, *, root: Path | None = None, db: Path | None = None) -> int:
    """Index exactly one file — the write-path fast lane.

    ``smriti_write`` used to run a full ``index_tree`` inside every
    session's MCP server: an embedding-model load plus a ~7000-file
    scan per write, with N concurrent servers contending for the one
    write lock (the "connection closed" era, ended 2026-09-06). This
    upserts a single source in one short transaction: chunk, embed
    only its chunks, delete-then-insert. Same leak guard as
    ``index_tree`` — never leave a transaction open on failure.

    Returns the number of chunks indexed.
    """
    if root is None:
        root = tree_root()
    if db is None:
        db = smriti_db_path()

    text = path.read_text(encoding="utf-8")
    rel_source = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")

    provider = _get_embedding_provider()
    chunks = chunk_markdown(text, source=rel_source)
    embeddings = asyncio.run(provider.embed([c.content for c in chunks])) if chunks else []

    conn = ensure_schema(db, provider.dimension)
    try:
        _delete_source(conn, rel_source)
        now = datetime.now(timezone.utc).isoformat()
        dist = trunk_distance(path, root)
        for i, c in enumerate(chunks):
            cid = compute_chunk_id(
                c.source, c.start_line, c.end_line, c.content_hash, provider.model_name
            )
            conn.execute(
                """INSERT OR REPLACE INTO chunks
                   (id, source, heading, heading_level, content,
                    start_line, end_line, content_hash, trunk_distance, indexed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cid, rel_source, c.heading, c.heading_level, c.content,
                 c.start_line, c.end_line, c.content_hash, dist, now),
            )
            rowid = conn.execute(
                "SELECT rowid FROM chunks WHERE id = ?", (cid,)
            ).fetchone()[0]
            if conn.has_vec:
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
                conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, _serialize_f32(embeddings[i])),
                )
        conn.commit()
        conn.close()
        return len(chunks)
    except BaseException:
        try:
            conn.rollback()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        raise

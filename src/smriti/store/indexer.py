"""Index the narada tree into the smriti SQLite store.

The indexer scans the tree for markdown files, chunks them, embeds the
chunks, and upserts into the database.  Incremental by default — only
files whose mtime or size differs from when they were last read are
re-processed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from smriti._vendored.memsearch.chunker import chunk_markdown, compute_chunk_id
from smriti._vendored.memsearch.scanner import scan_paths
from smriti.core.tree import smriti_db_path, tree_root, trunk_distance
from smriti.store.index_policy import is_indexable, may_hold_indexable
from smriti.store.schema import IndexDB, ensure_schema

log = logging.getLogger(__name__)


def _serialize_f32(vec: list[float]) -> bytes:
    """Pack a float list into a little-endian f32 blob for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


# Registry keys tried when nothing pins the choice, best-first.
_PROVIDER_AUTO_ORDER = ("onnx", "local")

# Map provider class name → registry key, for recording the selection
# in the index's meta table (the class doesn't know its registry key).
_PROVIDER_KEYS = {"OnnxEmbedding": "onnx", "LocalEmbedding": "local", "StubEmbedding": "stub"}


def provider_key(provider: object) -> str | None:
    """Registry key ("onnx"/"local"/"stub") for a provider instance, if known."""
    return _PROVIDER_KEYS.get(type(provider).__name__)


def _provider_for_model(model: str) -> str | None:
    """Legacy inference: which registry provider serves this model name."""
    name = model.lower()
    if name.startswith("stub-"):
        return "stub"
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
                return cast(str | None, row[0])
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


class _EmbeddingProvider(Protocol):
    """The embedding interface used here, across the untyped vendor boundary."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@functools.cache
def _get_embedding_provider() -> _EmbeddingProvider:
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
            return cast(_EmbeddingProvider, get_provider(key))
        except Exception as exc:
            last_error = exc
            log.warning("embedding provider %r unavailable: %s", key, exc)
    raise RuntimeError(
        f"No embedding provider available (tried {', '.join(keys)}; "
        f"last error: {last_error}). The default ONNX provider needs "
        "onnxruntime, tokenizers and huggingface_hub, all in "
        "pip install 'smriti[read]'; the local provider needs sentence-transformers."
    )


def _indexed_files(conn: IndexDB) -> dict[str, str]:
    """Return a mapping of source path → max(indexed_at) from the db."""
    rows = conn.execute(
        "SELECT source, MAX(indexed_at) FROM chunks GROUP BY source"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _recorded_sources(conn: IndexDB) -> dict[str, tuple[int, int]]:
    """Return a mapping of source path → (mtime_ns, size) when it was last read."""
    rows = conn.execute("SELECT source, mtime_ns, size FROM sources").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _disk_record(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of ``path`` now, or None if it's gone."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _record_source(conn: IndexDB, source: str, st: os.stat_result) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sources (source, mtime_ns, size) VALUES (?, ?, ?)",
        (source, st.st_mtime_ns, st.st_size),
    )


def _delete_source(conn: IndexDB, source: str) -> None:
    """Remove all chunks (and their vec/fts rows) and the record for a given source."""
    conn.execute("DELETE FROM sources WHERE source = ?", (source,))
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
            "DELETE FROM chunks WHERE rowid IN ("
            "SELECT rowid FROM chunks WHERE source = ?)",
            (source,),
        )


def _insert_chunk(conn: IndexDB, cid: str, chunk: Any, source: str, distance: int,
                  indexed_at: str, vector: list[float]) -> None:
    """Insert one chunk and its vector: the one insert path for index_tree and index_file."""
    conn.execute(
        """INSERT OR REPLACE INTO chunks
           (id, source, heading, heading_level, content,
            start_line, end_line, content_hash, trunk_distance, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, source, chunk.heading, chunk.heading_level, chunk.content,
         chunk.start_line, chunk.end_line, chunk.content_hash, distance, indexed_at),
    )
    if not conn.has_vec:
        return
    rowid = conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
    # Guard against orphaned vec rows: INSERT OR REPLACE on chunks frees a
    # rowid without any trigger cleaning its chunks_vec row (vec0 has no
    # triggers), and crash-era partial commits left such orphans behind.
    # When SQLite recycles one of those rowids the plain INSERT hits "UNIQUE
    # constraint failed on chunks_vec" — which crashed every incremental
    # index from mid-July until 2026-09-06. Delete-then-insert is idempotent.
    conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
    conn.execute(
        "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
        (rowid, _serialize_f32(vector)),
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
        if meta_dim != dim and not full:
            raise RuntimeError(
                f"index dimension is {meta_dim} but provider "
                f"{provider.model_name!r} embeds at {dim}; run "
                "'smriti index --full' to migrate the index"
            )

        # ── Scan tree ────────────────────────────────────────────────────
        root_resolved = root.resolve()
        scanned_files = [
            f for f in scan_paths(
                [str(root)],
                keep_dir=lambda d: may_hold_indexable(str(d.relative_to(root_resolved))),
            )
            if is_indexable(str(f.path.relative_to(root_resolved)))
        ]
        stats["scanned"] = len(scanned_files)
        log.info("Scanned %d files", len(scanned_files))

        # ── Purge sources that are gone or now excluded ──────────────────
        # Judged against the disk, not this scan, so a scan that misses a
        # directory once can't wipe its chunks. A full run replaces
        # everything anyway.
        indexed = _indexed_files(conn)
        recorded = _recorded_sources(conn)
        stats["purged"] = 0
        if not full:
            for source in sorted(set(indexed) | set(recorded)):
                if not is_indexable(source) or not (root / source).is_file():
                    _delete_source(conn, source)
                    indexed.pop(source, None)
                    recorded.pop(source, None)
                    stats["purged"] += 1
            if stats["purged"]:
                conn.commit()
                log.info("Purged %d sources (deleted or excluded)", stats["purged"])

        # ── Determine which files need (re-)indexing ─────────────────────
        # Each file is stat'ed before it is read, and that stat is what
        # gets recorded: an edit made after the read leaves the file
        # newer than its record, so the next run picks it up.
        to_index: list[tuple[Path, str, os.stat_result]] = []
        adopted: list[tuple[str, os.stat_result]] = []

        for sf in scanned_files:
            rel_source = str(sf.path.relative_to(root_resolved)).replace("\\", "/")
            try:
                st = sf.path.stat()
            except OSError:
                continue  # gone since the scan; the next run purges it
            if not full:
                was = recorded.get(rel_source)
                if was is not None:
                    if was == (st.st_mtime_ns, st.st_size):
                        stats["skipped"] += 1
                        continue
                elif rel_source in indexed:
                    # Indexed before sources were recorded: trust the old
                    # mtime test this once, and record the file as it is.
                    prev_dt = datetime.fromisoformat(indexed[rel_source])
                    if datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) <= prev_dt:
                        adopted.append((rel_source, st))
                        stats["skipped"] += 1
                        continue
            to_index.append((sf.path, rel_source, st))

        if to_index:
            log.info("Indexing %d files (%d skipped)", len(to_index), stats["skipped"])
        else:
            log.info("Nothing to index (all files up to date)")

        # ── Chunk all files ──────────────────────────────────────────────
        all_chunks = []
        read_sources: list[tuple[str, os.stat_result]] = []

        for fpath, rel_source, st in to_index:
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
            read_sources.append((rel_source, st))

        # ── Embed, with no write lock held ───────────────────────────────
        texts = [c.content for _, c, _, _ in all_chunks]
        if texts:
            log.info("Embedding %d chunks...", len(texts))
        embeddings = asyncio.run(provider.embed(texts)) if texts else []

        # ── Write, in one transaction ────────────────────────────────────
        # Searches keep seeing the old index until this commits, and a run
        # that fails anywhere leaves it as it was: a full run used to
        # commit its delete-everything before the embed (minutes), so
        # search found nothing meanwhile, and nothing at all if it crashed.
        now = datetime.now(timezone.utc).isoformat()
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # Other writers (index_file, behind every smriti_write) may have
        # published sources while this run embedded. Read under the write
        # lock. A later commit isn't a later read, so the disk decides: for
        # a source someone else re-recorded, this run's read replaces theirs
        # only when it is exactly the file as it is now. Otherwise theirs
        # stays (it may be newer), and if neither is current the next run,
        # finding the disk unlike the record, indexes it again.
        now_recorded = _recorded_sources(conn)
        ours = {src: (st.st_mtime_ns, st.st_size) for src, st in read_sources}
        keep = {
            s for s, rec in now_recorded.items()
            if rec != recorded.get(s)
            and (s not in ours or _disk_record(root / s) != ours[s])
        }
        if full and conn.has_vec and meta_dim != dim:
            # Every stored vector is the wrong size, so everything goes,
            # including what others published meanwhile; their records go
            # with them, so the next run embeds them again.
            keep = set()
            conn.execute("DROP TABLE chunks_vec")
            conn.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{dim}])")
        if full and not keep:
            conn.execute("DELETE FROM chunks")
            if conn.has_vec:
                conn.execute("DELETE FROM chunks_vec")
            if conn.has_fts:
                # The delete trigger already removed each row; this clears
                # any entries an old crash left without a chunk.
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('delete-all')")
            conn.execute("DELETE FROM sources")
        else:
            replaced = (set(_indexed_files(conn)) | set(now_recorded) if full
                        else {src for src, _ in read_sources})
            for src in replaced - keep:
                _delete_source(conn, src)
        if full:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('dimension', ?)",
                (str(dim),),
            )
        written = [(src, st) for src, st in adopted + read_sources if src not in keep]
        for src, st in written:
            _record_source(conn, src, st)
        pending = [(chunk, vector) for chunk, vector in zip(all_chunks, embeddings, strict=True)
                   if chunk[2] not in keep]

        for (cid, c, rel_source, fpath), vector in pending:
            _insert_chunk(conn, cid, c, rel_source, trunk_distance(fpath, root), now, vector)

        stats["indexed"] = sum(1 for src, _ in read_sources if src not in keep)
        stats["chunks"] = len(pending)

        # ── Update metadata ──────────────────────────────────────────────
        key = provider_key(provider)
        if key:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('embedding_provider', ?)",
                (key,),
            )
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
        metrics.log(
            "index_completed",
            **stats,
            elapsed_ms=int((_time.monotonic() - t0) * 1000),
            model=provider.model_name,
            dimension=dim,
        )
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

    Two sessions writing one file can finish out of order, so the same
    rule as ``index_tree`` decides: when another writer has recorded the
    source differently, this read replaces theirs only if it is the file
    as it is now.

    Returns the number of chunks indexed (0 when another read stands).
    """
    if root is None:
        root = tree_root()
    if db is None:
        db = smriti_db_path()

    rel_source = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    if not is_indexable(rel_source):
        return 0  # the same policy as index_tree; its purge removes any old chunks
    st = path.stat()  # before the read: an edit after it leaves the file newer than its record
    text = path.read_text(encoding="utf-8")

    provider = _get_embedding_provider()
    chunks = chunk_markdown(text, source=rel_source)
    embeddings = asyncio.run(provider.embed([c.content for c in chunks])) if chunks else []

    conn = ensure_schema(db, provider.dimension)
    try:
        mine = (st.st_mtime_ns, st.st_size)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT mtime_ns, size FROM sources WHERE source = ?", (rel_source,)
        ).fetchone()
        if row is not None and tuple(row) != mine and _disk_record(path) != mine:
            log.info("index_file %s: the file moved on since this read; the indexed "
                     "read stands, and the next index run catches up", rel_source)
            conn.rollback()
            conn.close()
            return 0
        _delete_source(conn, rel_source)
        _record_source(conn, rel_source, st)
        now = datetime.now(timezone.utc).isoformat()
        dist = trunk_distance(path, root)
        for c, vector in zip(chunks, embeddings, strict=True):
            cid = compute_chunk_id(
                c.source, c.start_line, c.end_line, c.content_hash, provider.model_name
            )
            _insert_chunk(conn, cid, c, rel_source, dist, now, vector)
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

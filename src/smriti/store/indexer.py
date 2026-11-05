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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from smriti._vendored.memsearch.chunker import chunk_markdown, compute_chunk_id
from smriti._vendored.memsearch.scanner import scan_paths
from smriti.core.tree import smriti_db_path, tree_root, trunk_distance
from smriti.embed.identity import (
    Embedded,
    EmbeddingIdentity,
    EmbeddingUnavailable,
    StaleEmbedding,
    bind_identity,
    check_vectors,
    identity_of,
    mismatched_identity,
    provider_for_model,
    provider_key,
    recorded_identity,
    vec_table_dimension,
    write_identity,
)
from smriti.store.index_policy import is_indexable, may_hold_indexable
from smriti.store.schema import IndexDB, ensure_schema

log = logging.getLogger(__name__)

__all__ = ["FileIndexed", "fill_pending_vectors", "index_file", "index_file_result",
           "index_tree", "provider_key"]

_provider_for_model = provider_for_model  # the older name


def _serialize_f32(vec: list[float]) -> bytes:
    """Pack a float list into a little-endian f32 blob for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


# Registry keys tried when nothing pins the choice, best-first.
_PROVIDER_AUTO_ORDER = ("onnx", "local")


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
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not read the index's embedding provider from %s: %s", db, exc)
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


class _InProcess:
    """A provider loaded in this process, with its identity worked out once, at load."""

    def __init__(self, inner: _EmbeddingProvider) -> None:
        self._inner = inner
        self.identity = identity_of(inner)

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._inner.embed(texts)


_IN_PROCESS: ContextVar[bool] = ContextVar("smriti_in_process_embeddings", default=False)


@contextmanager
def in_process_embeddings() -> Iterator[None]:
    """Embed with a model loaded in this process for the block, not through the service.

    ``index_tree`` runs this way: it embeds thousands of chunks in one
    short-lived process, where loading the model once costs less than
    shipping every chunk to the service.
    """
    token = _IN_PROCESS.set(True)
    try:
        yield
    finally:
        _IN_PROCESS.reset(token)


def _get_embedding_provider() -> _EmbeddingProvider:
    """The embedder for this process's searches and writes.

    In a long-lived process (the MCP server, ``smriti read``, hooks) that
    is the memory root's shared embedding service, so no process but the
    service loads a model (``smriti.embed``). ``SMRITI_EMBED_SERVICE=0``,
    or :func:`in_process_embeddings`, loads one here instead.
    """
    if not _IN_PROCESS.get():
        from smriti.embed.client import service_enabled, service_provider

        if service_enabled():
            return service_provider(tree_root())
    return _local_provider()


@functools.cache
def _local_provider() -> _EmbeddingProvider:
    """Load the embedding provider for this index in this process (cached).

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
            return _InProcess(cast(_EmbeddingProvider, get_provider(key)))
        except Exception as exc:  # noqa: BLE001 — logged; the next provider is tried
            last_error = exc
            log.warning("embedding provider %r unavailable: %s", key, exc)
    raise RuntimeError(
        f"No embedding provider available (tried {', '.join(keys)}; "
        f"last error: {last_error}). The default ONNX provider needs "
        "onnxruntime, tokenizers and huggingface_hub, all in "
        "pip install 'smriti[read]'; the local provider needs sentence-transformers."
    )


def _forget_provider() -> None:
    """Drop the cached model, so the next embedding loads what the index records now.

    A model cached in a long-lived process can outlive a migration of the
    index to another one; its vectors are then stale (see ``_embed``).
    """
    _local_provider.cache_clear()


def _embed(provider: _EmbeddingProvider, texts: list[str], kind: str = "write") -> Embedded:
    """Embed ``texts``, with the identity that made the vectors.

    A provider that knows the identity of each result (the embedding
    service's client) reports it per call; an in-process one has one
    identity for its life. ``kind`` ("query" or "write") sets the deadline
    a remote embedder works to.
    """
    per_call = getattr(provider, "embed_with_identity", None)
    if callable(per_call):
        embedded = per_call(texts, kind=kind)
        if not isinstance(embedded, Embedded):
            raise EmbeddingUnavailable("the embedder returned no identity with its vectors")
    else:
        vectors = asyncio.run(provider.embed(texts)) if texts else []
        embedded = Embedded(vectors, identity_of(provider))
    return check_vectors(embedded, len(texts))


def _reason(exc: BaseException) -> str:
    """A failure, as the user reads it in a degraded status."""
    if isinstance(exc, (EmbeddingUnavailable, StaleEmbedding)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


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


def _pending_sources(conn: IndexDB) -> set[str]:
    """Sources indexed for keyword search whose vectors are still to be made."""
    return {r[0] for r in conn.execute("SELECT source FROM sources WHERE vectors = 0")}


def _disk_record(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of ``path`` now, or None if it's gone."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _record_source(conn: IndexDB, source: str, st: os.stat_result, *,
                   vectors: bool = True) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sources (source, mtime_ns, size, vectors) VALUES (?, ?, ?, ?)",
        (source, st.st_mtime_ns, st.st_size, 1 if vectors else 0),
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
                  indexed_at: str, vector: list[float] | None) -> None:
    """Insert one chunk and its vector: the one insert path for index_tree and index_file.

    A chunk whose vector is pending (None) is still indexed for keyword search.
    """
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
    if vector is not None:
        _insert_vector(conn, rowid, vector)


def _insert_vector(conn: IndexDB, rowid: int, vector: Sequence[float]) -> None:
    conn.execute(
        "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
        (rowid, _serialize_f32(list(vector))),
    )


_migrating = mismatched_identity


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
    # A model in this process, not the service: a run embeds thousands of
    # chunks at once (see in_process_embeddings).
    with in_process_embeddings():
        provider = _get_embedding_provider()
    ours = identity_of(provider)
    dim = provider.dimension
    log.info("Embedding provider: %s (dimension=%d)", provider.model_name, dim)

    # ── Open / create database ───────────────────────────────────────
    conn = ensure_schema(db, dim)
    try:
        # Identity guard: vectors from two models can't share an index.
        # Different dimensions fail every vector insert (chunks_vec has a
        # fixed size that CREATE IF NOT EXISTS silently keeps); equal ones
        # insert happily and make search compare unrelated spaces. A full
        # re-index migrates to the new provider; an incremental run must
        # refuse loudly instead of half-writing.
        migrating = _migrating(conn, ours)
        if migrating is not None and not full:
            raise RuntimeError(
                f"the index was built with {migrating.describe()} but this run "
                f"embeds with {ours.describe()}; run 'smriti index --full' to "
                "migrate the index"
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
        # A source whose vectors are pending is read and embedded again,
        # unchanged or not: this run is one of the ways its vectors arrive.
        pending = _pending_sources(conn)

        for sf in scanned_files:
            rel_source = str(sf.path.relative_to(root_resolved)).replace("\\", "/")
            try:
                st = sf.path.stat()
            except OSError:
                continue  # gone since the scan; the next run purges it
            if not full:
                was = recorded.get(rel_source)
                if was is not None:
                    if was == (st.st_mtime_ns, st.st_size) and rel_source not in pending:
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
            except (OSError, UnicodeDecodeError) as exc:
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
        embeddings = _embed(provider, texts).vectors if texts else []

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
        ours_read = {src: (st.st_mtime_ns, st.st_size) for src, st in read_sources}
        keep = {
            s for s, rec in now_recorded.items()
            if rec != recorded.get(s)
            and (s not in ours_read or _disk_record(root / s) != ours_read[s])
        }
        # Checked again under the write lock: another writer may have given
        # the index its first vectors, or migrated it, while this run embedded.
        migrating = _migrating(conn, ours)
        if migrating is not None and not full:
            raise StaleEmbedding(
                f"the index moved to {migrating.describe()} while this run embedded with "
                f"{ours.describe()}; nothing was written. Run 'smriti index' again")
        resize = conn.has_vec and vec_table_dimension(conn) != dim
        if full and (migrating is not None or resize):
            # Every stored vector is another model's (or the wrong size), so
            # everything goes, including what others published meanwhile;
            # their records go with them, so the next run embeds them again.
            keep = set()
            if conn.has_vec:
                conn.execute("DROP TABLE IF EXISTS chunks_vec")
                conn.execute(
                    f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{dim}])")
        # The index's identity, recorded in the transaction that writes these vectors.
        if full and migrating is not None:
            write_identity(conn, ours)
        else:
            bind_identity(conn, ours)
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
        written = [(src, st) for src, st in adopted + read_sources if src not in keep]
        for src, st in written:
            _record_source(conn, src, st)
        to_write = [(chunk, vector) for chunk, vector in zip(all_chunks, embeddings, strict=True)
                    if chunk[2] not in keep]

        for (cid, c, rel_source, fpath), vector in to_write:
            _insert_chunk(conn, cid, c, rel_source, trunk_distance(fpath, root), now, vector)

        stats["indexed"] = sum(1 for src, _ in read_sources if src not in keep)
        stats["chunks"] = len(to_write)

        # ── Update metadata ──────────────────────────────────────────────
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
            (now,),
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
            except sqlite3.Error as exc:  # the original failure is what propagates
                log.warning("could not close the index after a failure: %s", exc)
        raise


@dataclass(frozen=True)
class FileIndexed:
    """What ``index_file`` did with one file."""

    chunks: int  # chunks written; 0 when another writer's newer read stands
    vectors_pending: str = ""  # why the chunks' vectors are pending, or "" when they are in
    others_pending: bool = False  # other sources' vectors are pending too


def index_file(path: Path, *, root: Path | None = None, db: Path | None = None) -> int:
    """Index exactly one file; returns the chunks written (see :func:`index_file_result`)."""
    return index_file_result(path, root=root, db=db).chunks


def index_file_result(path: Path, *, root: Path | None = None,
                      db: Path | None = None) -> FileIndexed:
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

    When no vectors can be made (the embedder is unavailable, or its
    vectors are of a model the index doesn't record, even on a fresh try),
    the chunks are still indexed for keyword search at once and the source
    is recorded with its vectors pending; repairs fill them in later
    (:func:`fill_pending_vectors`). A successful write fills a few pending
    sources itself.
    """
    if root is None:
        root = tree_root()
    if db is None:
        db = smriti_db_path()

    rel_source = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    if not is_indexable(rel_source):
        return FileIndexed(0)  # the same policy as index_tree; its purge removes old chunks
    st = path.stat()  # before the read: an edit after it leaves the file newer than its record
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, source=rel_source)
    texts = [c.content for c in chunks]

    embedded, reason = _try_embed(texts)
    try:
        result = _commit_file(db, path, root, rel_source, st, chunks, embedded, reason)
    except StaleEmbedding as exc:
        # The vectors are of a model the index no longer records (or never
        # did): stale work. Embed once more, with a freshly loaded model.
        log.info("index_file %s: %s; embedding again", rel_source, exc)
        _forget_provider()
        embedded, reason = _try_embed(texts)
        try:
            result = _commit_file(db, path, root, rel_source, st, chunks, embedded, reason)
        except StaleEmbedding as again:
            log.warning("index_file %s: %s; indexed for keyword search, vectors pending",
                        rel_source, again)
            result = _commit_file(db, path, root, rel_source, st, chunks, None,
                                  f"the embedder doesn't match the index: {again}")
    if result.others_pending and embedded is not None and not result.vectors_pending:
        _fill_some_pending(db)
    return result


def _try_embed(texts: list[str]) -> tuple[Embedded | None, str]:
    """Vectors for ``texts``, or None and why not."""
    if not texts:
        return None, ""
    try:
        return _embed(_get_embedding_provider(), texts, "write"), ""
    except Exception as exc:  # noqa: BLE001 — logged; the write is indexed for keywords meanwhile
        log.warning("embedding failed; the write's vectors are pending: %s", exc)
        return None, _reason(exc)


def _commit_file(db: Path, path: Path, root: Path, rel_source: str, st: os.stat_result,
                 chunks: list[Any], embedded: Embedded | None, reason: str) -> FileIndexed:
    """Write one file's chunks, and its vectors when there are any, in one transaction.

    Raises :class:`StaleEmbedding`, having written nothing, when the vectors'
    identity isn't the index's.
    """
    dim = embedded.identity.dimension if embedded is not None else 0
    conn = ensure_schema(db, dim or None)
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
            return FileIndexed(0)
        if embedded is not None:
            identity: EmbeddingIdentity | None = bind_identity(conn, embedded.identity)
        else:
            identity = recorded_identity(conn)
        pending = bool(chunks) and embedded is None and conn.has_vec
        _delete_source(conn, rel_source)
        _record_source(conn, rel_source, st, vectors=not pending)
        now = datetime.now(timezone.utc).isoformat()
        dist = trunk_distance(path, root)
        model = identity.model if identity is not None else ""
        vectors: list[list[float] | None] = (
            list(embedded.vectors) if embedded is not None else [None] * len(chunks))
        for c, vector in zip(chunks, vectors, strict=True):
            cid = compute_chunk_id(c.source, c.start_line, c.end_line, c.content_hash, model)
            _insert_chunk(conn, cid, c, rel_source, dist, now, vector)
        others = conn.execute(
            "SELECT 1 FROM sources WHERE vectors = 0 AND source != ? LIMIT 1", (rel_source,)
        ).fetchone() is not None
        conn.commit()
        conn.close()
        return FileIndexed(len(chunks), reason if pending else "", others)
    except BaseException:
        try:
            conn.rollback()
        finally:
            try:
                conn.close()
            except sqlite3.Error as exc:  # the original failure is what propagates
                log.warning("could not close the index after a failure: %s", exc)
        raise


def _fill_some_pending(db: Path) -> None:
    """After a write that made its vectors, fill a few pending sources too (best effort)."""
    try:
        fill_pending_vectors(db=db, max_chunks=32)
    except Exception as exc:  # noqa: BLE001 — logged; the service and index runs repair too
        log.warning("filling pending vectors after a write failed: %s", exc)


def fill_pending_vectors(*, db: Path | None = None,
                         embed: Callable[[list[str]], Embedded] | None = None,
                         max_chunks: int = 64, busy_ms: int | None = None) -> int:
    """Make the vectors of sources indexed while the embedder was unavailable.

    Takes pending sources in order until ``max_chunks`` chunks (always at
    least one source), embeds their chunks as they are in the index, with
    no lock held, then writes the vectors in one transaction, checking the
    identity there. A source rewritten meanwhile is left to its writer.
    Returns how many sources now have their vectors. Raises
    :class:`StaleEmbedding` when the vectors aren't the index's model, and
    whatever ``embed`` raises. ``busy_ms`` bounds the wait for another
    writer (the service keeps it short, so a repair never holds up queries).
    """
    if db is None:
        db = smriti_db_path()
    if not db.exists():
        return 0
    conn = ensure_schema(db, None, busy_ms=busy_ms)
    try:
        if not conn.has_vec:
            return 0
        picked: list[tuple[str, tuple[int, int], list[tuple[int, str]], list[str]]] = []
        total = 0
        for source, mtime_ns, size in conn.execute(
                "SELECT source, mtime_ns, size FROM sources WHERE vectors = 0 "
                "ORDER BY source LIMIT 256").fetchall():
            rows = conn.execute("SELECT rowid, id, content FROM chunks WHERE source = ? "
                                "ORDER BY rowid", (source,)).fetchall()
            if picked and total + len(rows) > max_chunks:
                break
            picked.append((source, (mtime_ns, size), [(r[0], r[1]) for r in rows],
                           [r[2] for r in rows]))
            total += len(rows)
        if not picked:
            return 0
        texts = [t for _, _, _, contents in picked for t in contents]
        embedded: Embedded | None = None
        if texts:
            embedded = (embed or (lambda ts: _embed(_get_embedding_provider(), ts)))(texts)
            check_vectors(embedded, len(texts))
        conn.execute("BEGIN IMMEDIATE")
        if embedded is not None:
            bind_identity(conn, embedded.identity)
        filled, at = 0, 0
        for source, record, ids, _ in picked:
            vecs = embedded.vectors[at:at + len(ids)] if embedded is not None else []
            at += len(ids)
            now = conn.execute("SELECT mtime_ns, size, vectors FROM sources WHERE source = ?",
                               (source,)).fetchone()
            current = [(r[0], r[1]) for r in conn.execute(
                "SELECT rowid, id FROM chunks WHERE source = ? ORDER BY rowid", (source,))]
            if now is None or (now[0], now[1]) != record or now[2] != 0 or current != ids:
                continue  # rewritten since it was read: its writer owns it now
            for (rowid, _), vector in zip(ids, vecs, strict=True):
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
                _insert_vector(conn, rowid, vector)
            conn.execute("UPDATE sources SET vectors = 1 WHERE source = ?", (source,))
            filled += 1
        conn.commit()
        if filled:
            log.info("filled the pending vectors of %d sources", filled)
        return filled
    except BaseException:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error as exc:
            log.warning("could not close the index: %s", exc)

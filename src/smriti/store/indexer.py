"""Index the narada tree into the smriti SQLite store.

The indexer scans the tree for markdown files, chunks them, embeds the
chunks, and upserts into the database.  Incremental by default — only
files whose mtime or size differs from when they were last read are
re-processed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smriti._vendored.memsearch.chunker import chunk_markdown, compute_chunk_id
from smriti._vendored.memsearch.scanner import scan_paths
from smriti.core.tree import index_db_path, smriti_db_path, tree_root, trunk_distance
from smriti.embed import provider as embedding
from smriti.embed.identity import (
    Embedded,
    EmbeddingIdentity,
    StaleEmbedding,
    bind_identity,
    check_vectors,
    identity_of,
    mismatched_identity,
    provider_key,
    recorded_identity,
    vec_table_dimension,
    write_identity,
)
from smriti.store.index_policy import is_indexable, may_hold_indexable
from smriti.store.schema import IndexDB, ensure_schema, serialize_f32

log = logging.getLogger(__name__)

__all__ = ["FileIndexed", "fill_pending_vectors", "index_file", "index_file_result",
           "index_tree", "provider_key"]

# Embedding goes through smriti.embed.provider (the embedder, the provider
# rule, the identity of each batch of vectors), which search and the
# embedding service share.


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


# A chunk as index_tree stages it for its write: id, source, heading, heading
# level, content, start and end line, content hash, trunk distance, and its
# vector as sqlite-vec stores it (None while pending).
_Row = tuple[str, str, str, int, str, int, int, str, int, bytes | None]


def _row(cid: str, chunk: Any, source: str, distance: int, vector: bytes | None) -> _Row:
    return (cid, source, chunk.heading, chunk.heading_level, chunk.content,
            chunk.start_line, chunk.end_line, chunk.content_hash, distance, vector)


def _insert_chunk(conn: IndexDB, cid: str, chunk: Any, source: str, distance: int,
                  indexed_at: str, vector: list[float] | None) -> bool:
    """Insert one chunk and its vector (index_file's path; see :func:`_insert_row`)."""
    blob = serialize_f32(vector) if vector is not None else None
    return _insert_row(conn, _row(cid, chunk, source, distance, blob), indexed_at)


def _insert_row(conn: IndexDB, row: _Row, indexed_at: str) -> bool:
    """Insert one chunk and its vector: the one insert path for index_tree and index_file.

    A chunk whose vector is pending (None) is still indexed for keyword search.
    A chunk of the same id (one file can make two alike: the same lines, the
    same text) is replaced; returns True when one was.
    """
    cid, source, heading, level, content, start, end, content_hash, distance, blob = row
    # Delete, then insert. INSERT OR REPLACE removed the old row without
    # firing the FTS delete trigger (SQLite fires none for REPLACE unless
    # recursive_triggers is on), so the keyword index kept an entry for a
    # row that was gone; vec0 has no triggers at all.
    old = conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()
    if old is not None:
        if conn.has_vec:
            conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (old[0],))
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (old[0],))
    conn.execute(
        """INSERT INTO chunks
           (id, source, heading, heading_level, content,
            start_line, end_line, content_hash, trunk_distance, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, source, heading, level, content, start, end, content_hash, distance, indexed_at),
    )
    if not conn.has_vec:
        return old is not None
    rowid = conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
    # Guard against orphaned vec rows: an index written by an older version
    # (INSERT OR REPLACE above, or a write in parts) can hold a chunks_vec row
    # for a rowid SQLite now recycles, and the plain INSERT would hit "UNIQUE
    # constraint failed on chunks_vec" and fail the whole incremental index.
    # Delete-then-insert is idempotent.
    conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (rowid,))
    if blob is not None:
        conn.execute("INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)", (rowid, blob))
    return old is not None


def _insert_vector(conn: IndexDB, rowid: int, vector: Sequence[float]) -> None:
    conn.execute(
        "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
        (rowid, serialize_f32(vector)),
    )


_migrating = mismatched_identity

# The most chunks index_tree embeds in one call, and holds in memory before
# its write. A multiple of each in-process provider's own batch (onnx 32,
# local 512), so every model call sees the texts it saw when a run embedded
# all its chunks in one call, and the vectors are the same. (The onnx
# provider splits a batch over its attention bound by length, which depends
# on that batch's texts only.)
EMBED_BATCH = 512


class _Staged:
    """The chunks a run has embedded, waiting for its write transaction.

    Up to one batch they stay in memory, which covers an incremental run's
    usual few files; past that they go to a private temporary SQLite
    database on disk (SQLite deletes it on close), so a full index holds one
    batch of chunks and vectors at a time however large the tree.
    """

    def __init__(self, batch: int) -> None:
        self._batch = batch
        self._rows: list[_Row] = []
        self._db: sqlite3.Connection | None = None

    def add(self, rows: list[_Row]) -> None:
        if self._db is None and len(self._rows) + len(rows) <= self._batch:
            self._rows.extend(rows)
            return
        if self._db is None:
            self._db = sqlite3.connect("")  # "": a temporary database on disk
            self._db.execute("PRAGMA journal_mode=OFF")
            self._db.execute("PRAGMA synchronous=OFF")
            self._db.execute("CREATE TABLE staged (id, source, heading, heading_level, "
                             "content, start_line, end_line, content_hash, distance, vector)")
            rows, self._rows = self._rows + rows, []
        self._db.executemany("INSERT INTO staged VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._db.commit()

    def rows(self) -> Iterator[_Row]:
        """Every staged chunk, in the order it was embedded."""
        if self._db is None:
            yield from self._rows
            return
        yield from self._db.execute("SELECT * FROM staged ORDER BY rowid")

    def close(self) -> None:
        self._rows = []
        if self._db is not None:
            self._db.close()
            self._db = None


@dataclass
class _Plan:
    """What a run reads: the files it indexes, those it adopts unread, the
    sources it purges, and the records it began from (for the reconcile under
    the write lock)."""

    to_index: list[tuple[Path, str, os.stat_result]]
    adopted: list[tuple[str, os.stat_result]]
    recorded: dict[str, tuple[int, int]]
    purge: list[str]


def index_tree(
    *,
    full: bool = False,
    root: Path | None = None,
    db: Path | None = None,
    verbose: bool = False,
    busy_ms: int | None = None,
) -> dict[str, int]:
    """Index the narada tree: scan, plan, embed, commit.

    Embedding runs in batches of at most :data:`EMBED_BATCH` chunks with no
    lock held, staging each batch (:class:`_Staged`); the write is one
    transaction under ``BEGIN IMMEDIATE``, reconciled against the disk with
    what other writers recorded meanwhile. So searches keep the old index
    until it commits, a run that fails anywhere leaves it as it was, and a
    full run's memory stays flat however large the tree.

    Parameters
    ----------
    full:
        If True, drop all existing data and re-index from scratch.
    root:
        Tree root (defaults to ``tree_root()``).
    db:
        Database path (defaults to the root's, ``<root>/.smriti/index.db``).
    verbose:
        Log progress at INFO level.
    busy_ms:
        How long the write waits for another writer's lock; by default
        ``SMRITI_DB_BUSY_TIMEOUT_MS`` or 10 s (see :func:`ensure_schema`).

    Returns
    -------
    dict with keys: scanned, skipped, indexed, chunks, errors, purged
    """
    import time as _time
    from smriti.metrics import logger_for

    t0 = _time.monotonic()

    # Everything follows the root: its index, the provider that index records,
    # and its metrics. Never the environment's root, when another is given.
    if root is None:
        root = tree_root()
    if db is None:
        db = index_db_path(root)

    stats = {"scanned": 0, "skipped": 0, "indexed": 0, "chunks": 0, "errors": 0}

    # ── Get embedding provider and dimension ─────────────────────────
    # A model in this process, not the service: a run embeds thousands of
    # chunks at once (see in_process_embeddings). A full run rebuilds the
    # index, so SMRITI_EMBED_PROVIDER may choose its model (provider_keys).
    with embedding.in_process_embeddings(db=db, rebuild=full):
        provider = embedding.embedder()
    ours = identity_of(provider)
    dim = provider.dimension
    log.info("Embedding provider: %s (dimension=%d)", provider.model_name, dim)

    # ── Open / create database ───────────────────────────────────────
    conn = ensure_schema(db, dim, busy_ms=busy_ms)
    staged = _Staged(EMBED_BATCH)
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
        files = _scan(root)
        stats["scanned"] = len(files)
        log.info("Scanned %d files", len(files))
        plan = _plan(conn, files, root, full, stats)
        read_sources = _embed_files(provider, plan, root, staged, stats)
        _commit(conn, ours, dim, full, root, plan, read_sources, staged, stats)
        # Keep planner statistics fresh on the (growing) chunks table so
        # the source index actually gets picked. Cheap no-op when stats
        # are already current; runs on the nightly cadence.
        try:
            conn.execute("PRAGMA optimize")
        except sqlite3.OperationalError:
            pass
        conn.close()
    except BaseException:  # noqa: BLE001 — rolled back and closed on any exit; re-raised
        # Never leak an open write transaction from a long-lived
        # process: a mid-index crash with the connection left open
        # wedges every other writer of the index. Roll back and close
        # on ANY failure, then re-raise.
        try:
            conn.rollback()
        finally:
            try:
                conn.close()
            except sqlite3.Error as exc:  # the original failure is what propagates
                log.warning("could not close the index after a failure: %s", exc)
        raise
    finally:
        staged.close()

    log.info(
        "Indexed %d files → %d chunks (model=%s)",
        stats["indexed"],
        stats["chunks"],
        provider.model_name,
    )
    logger_for(root).log(
        "index_completed",
        **stats,
        elapsed_ms=int((_time.monotonic() - t0) * 1000),
        model=provider.model_name,
        dimension=dim,
    )
    return stats


def _scan(root: Path) -> list[Path]:
    """The indexable files under ``root``, walking only directories that may hold one."""
    root_resolved = root.resolve()
    return [
        f.path for f in scan_paths(
            [str(root)],
            keep_dir=lambda d: may_hold_indexable(str(d.relative_to(root_resolved))),
        )
        if is_indexable(str(f.path.relative_to(root_resolved)))
    ]


def _gone(root: Path, source: str) -> bool:
    """Whether an indexed source is to be purged: deleted, or now excluded.

    Judged against the disk, not a scan, so a scan that misses a directory
    once can't wipe its chunks.
    """
    return not is_indexable(source) or not (root / source).is_file()


def _plan(conn: IndexDB, files: list[Path], root: Path, full: bool,
          stats: dict[str, int]) -> _Plan:
    """Decide what this run purges and which files it reads; only reads the index.

    The purge is written with everything else, in the run's one transaction
    (:func:`_commit`), so a run that fails leaves it undone. A full run
    replaces everything anyway.
    """
    root_resolved = root.resolve()
    indexed = _indexed_files(conn)
    recorded = _recorded_sources(conn)
    stats["purged"] = 0
    purge = ([] if full else
             [s for s in sorted(set(indexed) | set(recorded)) if _gone(root, s)])

    # ── Determine which files need (re-)indexing ─────────────────────
    # Each file is stat'ed before it is read, and that stat is what
    # gets recorded: an edit made after the read leaves the file
    # newer than its record, so the next run picks it up.
    to_index: list[tuple[Path, str, os.stat_result]] = []
    adopted: list[tuple[str, os.stat_result]] = []
    # A source whose vectors are pending is read and embedded again,
    # unchanged or not: this run is one of the ways its vectors arrive.
    # Not without sqlite-vec, where its vectors still couldn't be stored.
    pending = _pending_sources(conn) if conn.has_vec else set()

    for path in files:
        rel_source = str(path.relative_to(root_resolved)).replace("\\", "/")
        try:
            st = path.stat()
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
        to_index.append((path, rel_source, st))

    if to_index:
        log.info("Indexing %d files (%d skipped)", len(to_index), stats["skipped"])
    else:
        log.info("Nothing to index (all files up to date)")
    return _Plan(to_index, adopted, recorded, purge)


def _embed_files(provider: embedding.EmbeddingProvider, plan: _Plan, root: Path,
                 staged: _Staged, stats: dict[str, int]) -> list[tuple[str, os.stat_result]]:
    """Read, chunk and embed the planned files, staging each batch; the sources read.

    With no lock held. A batch closes at every :data:`EMBED_BATCH` chunks of
    the run, across file boundaries, so the model is called on the same
    texts as by one call for every chunk.
    """
    read_sources: list[tuple[str, os.stat_result]] = []
    waiting: list[tuple[str, Any, str, int]] = []  # id, chunk, source, trunk distance
    embedded = 0

    def flush() -> None:
        nonlocal embedded
        vectors = embedding.embed(provider, [c.content for _, c, _, _ in waiting]).vectors
        staged.add([_row(cid, c, src, dist, serialize_f32(vector))
                    for (cid, c, src, dist), vector in zip(waiting, vectors, strict=True)])
        embedded += len(waiting)
        waiting.clear()

    for fpath, rel_source, st in plan.to_index:
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read %s: %s", fpath, exc)
            stats["errors"] += 1
            continue
        distance = trunk_distance(fpath, root)
        for c in chunk_markdown(text, source=rel_source):
            cid = compute_chunk_id(
                c.source, c.start_line, c.end_line, c.content_hash, provider.model_name
            )
            waiting.append((cid, c, rel_source, distance))
            if len(waiting) >= EMBED_BATCH:
                flush()
        read_sources.append((rel_source, st))
    if waiting:
        flush()
    if embedded:
        log.info("Embedded %d chunks", embedded)
    return read_sources


def _commit(conn: IndexDB, ours: EmbeddingIdentity, dim: int, full: bool, root: Path,
            plan: _Plan, read_sources: list[tuple[str, os.stat_result]], staged: _Staged,
            stats: dict[str, int]) -> None:
    """Write the run in one transaction, reconciled with what other writers recorded."""
    # Searches keep seeing the old index until this commits, and a run
    # that fails anywhere leaves it as it was: a full run's delete of
    # everything is in this transaction, never committed before the embed
    # (minutes), when search would find nothing, or nothing at all after
    # a crash.
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
        if rec != plan.recorded.get(s)
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
        # The purge: what is still gone, unless another writer recorded it since.
        for src in plan.purge:
            if src not in keep and _gone(root, src):
                _delete_source(conn, src)
                stats["purged"] += 1
        if stats["purged"]:
            log.info("Purged %d sources (deleted or excluded)", stats["purged"])
    written = [(src, st) for src, st in plan.adopted + read_sources if src not in keep]
    for src, st in written:
        _record_source(conn, src, st)
    chunks = 0  # the rows written: two alike chunks of one file are one row
    for row in staged.rows():
        if row[1] not in keep and not _insert_row(conn, row, now):
            chunks += 1
    if not conn.has_vec:
        # No vectors were stored: what this run wrote is pending, so the
        # repairs fill it in once sqlite-vec is installed.
        for src, _ in written:
            conn.execute("UPDATE sources SET vectors = 0 WHERE source = ? AND EXISTS "
                         "(SELECT 1 FROM chunks WHERE source = ?)", (src, src))

    stats["indexed"] = sum(1 for src, _ in read_sources if src not in keep)
    stats["chunks"] = chunks

    # ── Update metadata ──────────────────────────────────────────────
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
        (now,),
    )
    conn.commit()


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

    A full ``index_tree`` per ``smriti_write`` would be an embedding-model
    load plus a ~7000-file scan in every session's MCP server, with
    concurrent servers contending for the one write lock. This
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
    (:func:`fill_pending_vectors`): the embedding service's loop, between
    requests, and the nightly's :func:`index_tree`. A write never waits for
    other sources' vectors through the service. Embedding in this process
    (the service off), a successful write fills small pending sources
    itself, within :data:`WRITE_REPAIR_CHUNKS` chunks; a larger one is left
    to the nightly.

    The database, and the embedder (the root's service, or the provider its
    index records), are the root's: ``root`` when given, else the
    environment's. ``db`` overrides the database only.
    """
    if root is None:
        root = tree_root()
    if db is None:
        db = index_db_path(root)

    rel_source = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    if not is_indexable(rel_source):
        return FileIndexed(0)  # the same policy as index_tree; its purge removes old chunks
    st = path.stat()  # before the read: an edit after it leaves the file newer than its record
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, source=rel_source)
    texts = [c.content for c in chunks]

    with embedding.embedding_root(root):
        embedded, reason = _try_embed(texts)
        try:
            result = _commit_file(db, path, root, rel_source, st, chunks, embedded, reason)
        except StaleEmbedding as exc:
            # The vectors are of a model the index no longer records (or never
            # did): stale work. Embed once more, with a freshly loaded model.
            log.info("index_file %s: %s; embedding again", rel_source, exc)
            embedding.forget_provider()
            embedded, reason = _try_embed(texts)
            try:
                result = _commit_file(db, path, root, rel_source, st, chunks, embedded, reason)
            except StaleEmbedding as again:
                log.warning("index_file %s: %s; indexed for keyword search, vectors pending",
                            rel_source, again)
                result = _commit_file(db, path, root, rel_source, st, chunks, None,
                                      f"the embedder doesn't match the index: {again}")
        if (result.others_pending and embedded is not None and not result.vectors_pending
                and not embedding.embeds_through_service()):
            _fill_some_pending(db)
    return result


def _try_embed(texts: list[str]) -> tuple[Embedded | None, str]:
    """Vectors for ``texts``, or None and why not."""
    if not texts:
        return None, ""
    try:
        return embedding.embed(embedding.embedder(), texts, "write"), ""
    except Exception as exc:  # noqa: BLE001 — logged; the write is indexed for keywords meanwhile
        log.warning("embedding failed; the write's vectors are pending: %s", exc)
        return None, embedding.failure_reason(exc)


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
        # Pending means "no vectors stored", whatever the reason: without
        # sqlite-vec even good vectors can't be stored, and a source recorded
        # as having them would be skipped by every repair once it is installed.
        pending = bool(chunks) and (embedded is None or not conn.has_vec)
        if pending and embedded is not None:
            reason = NO_VEC_REASON
        _delete_source(conn, rel_source)
        _record_source(conn, rel_source, st, vectors=not pending)
        now = datetime.now(timezone.utc).isoformat()
        dist = trunk_distance(path, root)
        model = identity.model if identity is not None else ""
        vectors: list[list[float] | None] = (
            list(embedded.vectors) if embedded is not None else [None] * len(chunks))
        written = 0  # the rows: two alike chunks of one file are one
        for c, vector in zip(chunks, vectors, strict=True):
            cid = compute_chunk_id(c.source, c.start_line, c.end_line, c.content_hash, model)
            written += not _insert_chunk(conn, cid, c, rel_source, dist, now, vector)
        others = conn.execute(
            "SELECT 1 FROM sources WHERE vectors = 0 AND source != ? LIMIT 1", (rel_source,)
        ).fetchone() is not None
        conn.commit()
        conn.close()
        return FileIndexed(written, reason if pending else "", others)
    except BaseException:  # noqa: BLE001 — rolled back and closed on any exit; re-raised
        try:
            conn.rollback()
        finally:
            try:
                conn.close()
            except sqlite3.Error as exc:  # the original failure is what propagates
                log.warning("could not close the index after a failure: %s", exc)
        raise


NO_VEC_REASON = "sqlite-vec is not available here, so no vectors can be stored"


def _can_fill(db: Path) -> bool:
    """True when the index has pending sources and this process can store vectors.

    Read-only, so a repair loop that asks while sqlite-vec is missing costs a
    read, never a write transaction or an embedding.
    """
    from smriti.store.schema import open_readonly, pending_vectors

    conn = open_readonly(db)
    try:
        return conn.has_vec and pending_vectors(conn) > 0
    finally:
        conn.close()


WRITE_REPAIR_CHUNKS = 32  # the most other sources' chunks a write embeds in-process


def _fill_some_pending(db: Path) -> None:
    """After a write that made its vectors in-process, fill a few small pending sources
    too (best effort), strictly within :data:`WRITE_REPAIR_CHUNKS`."""
    try:
        fill_pending_vectors(db=db, max_chunks=WRITE_REPAIR_CHUNKS, strict=True)
    except Exception as exc:  # noqa: BLE001 — logged; the service and index runs repair too
        log.warning("filling pending vectors after a write failed: %s", exc)


def fill_pending_vectors(*, db: Path | None = None,
                         embed: Callable[[list[str]], Embedded] | None = None,
                         max_chunks: int = 64, busy_ms: int | None = None,
                         strict: bool = False) -> int:
    """Make the vectors of sources indexed while the embedder was unavailable.

    Takes pending sources in order until ``max_chunks`` chunks (always at
    least one source, unless ``strict``: then a source with more chunks than
    the budget has left is skipped, for a later repair, so a caller never
    embeds more than ``max_chunks``), embeds their chunks as they are in the
    index, with no lock held, then writes the vectors in one transaction,
    checking the identity there. A source rewritten meanwhile is left to its
    writer.
    Returns how many sources now have their vectors. Raises
    :class:`StaleEmbedding` when the vectors aren't the index's model, and
    whatever ``embed`` raises. ``busy_ms`` bounds the wait for another
    writer (the service keeps it short, so a repair never holds up queries).
    Without sqlite-vec nothing can be filled: it returns 0 at once, having
    only read the index, and the sources stay pending until it is installed.
    """
    if db is None:
        db = smriti_db_path()
    if not db.exists() or not _can_fill(db):
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
            if strict:
                (count,) = conn.execute("SELECT COUNT(*) FROM chunks WHERE source = ?",
                                        (source,)).fetchone()
                if total + count > max_chunks:
                    continue  # more than the budget has left; its content isn't read
            rows = conn.execute("SELECT rowid, id, content FROM chunks WHERE source = ? "
                                "ORDER BY rowid", (source,)).fetchall()
            if total + len(rows) > max_chunks and (picked or strict):
                if strict:
                    continue  # it grew since it was counted
                break
            picked.append((source, (mtime_ns, size), [(r[0], r[1]) for r in rows],
                           [r[2] for r in rows]))
            total += len(rows)
            if strict and total >= max_chunks:
                break
        if not picked:
            return 0
        texts = [t for _, _, _, contents in picked for t in contents]
        embedded: Embedded | None = None
        if texts:
            embedded = (embed or (lambda ts: embedding.embed(embedding.embedder(), ts)))(texts)
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
    except BaseException:  # noqa: BLE001 — rolled back on any exit (closed below); re-raised
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except sqlite3.Error as exc:
            log.warning("could not close the index: %s", exc)

"""Database schema for the smriti index.

Creates three linked structures:
- ``chunks`` table — main content with metadata
- ``chunks_fts`` FTS5 virtual table — keyword search
- ``chunks_vec`` vec0 virtual table — vector similarity search

The FTS5 and vec0 tables are linked to ``chunks`` by rowid.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 3: sources.vectors (a source whose vectors are pending) and the embedding
# identity in meta (smriti.embed.identity).
SCHEMA_VERSION = 3

# The size chunks_vec gets when it must exist before any vector does (a first
# write whose vectors are pending). The first vectors resize it to their own
# (smriti.embed.identity.bind_identity), so nothing is ever stored at this size
# unless that is the model's.
_PLACEHOLDER_DIM = 384


@dataclass
class IndexDB:
    """Wrapper around a sqlite3 connection with capability flags."""

    conn: sqlite3.Connection
    has_vec: bool
    has_fts: bool

    def execute(
        self, sql: str, parameters: Sequence[object] | Mapping[str, object] = (), /
    ) -> sqlite3.Cursor:
        return self.conn.execute(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self.conn.executescript(sql_script)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension.  Returns True on success."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:  # noqa: BLE001 — logged; search falls back to keywords
        log.warning("sqlite-vec not available: %s", exc)
        return False


def _has_fts5(conn: sqlite3.Connection) -> bool:
    """Check whether the sqlite3 build includes FTS5."""
    # An existing chunks_fts table IS the proof — skip the write-probe,
    # which added two DDL statements to every write-path ensure_schema.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone():
        return True
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def open_readonly(db_path: Path) -> IndexDB:
    """Read-only connection for search paths.

    Readers must never be failed by writers ("database is locked" under
    seven concurrent MCP servers, 2026-08-27): mode=ro cannot take a
    write lock, busy_timeout rides out checkpoints, and no schema work
    happens — the indexer owns the schema, searchers just read it.
    Capability flags come from *what exists in the index*, not from
    write-probes (the old FTS5 probe created a table, which a reader
    neither can nor should).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"no index at {db_path}")
    # Percent-encode the path: '#'/'?' are legal in directory names but
    # URI-reserved, and a raw interpolation would silently truncate the
    # path SQLite opens (Codex review P2).
    from urllib.parse import quote
    uri = "file:" + quote(str(db_path).replace("\\", "/"), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA query_only=ON")
    has_vec = _load_sqlite_vec(conn)
    has_fts = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone())
    return IndexDB(conn=conn, has_vec=has_vec, has_fts=has_fts)


def ensure_schema(
    db_path: Path,
    dimension: int | None,
) -> IndexDB:
    """Open (or create) the index database and ensure all tables exist.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Parent directories are created
        automatically.
    dimension:
        Embedding vector dimension (set at schema creation time; changing it
        requires a full re-index). None when the caller has no vectors to
        write (their embedding failed): nothing is recorded, and the first
        vectors set the size.

    Returns
    -------
    IndexDB
        A wrapped connection with WAL mode, extensions loaded, and
        capability flags.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Writer busy timeout, overridable for batch contexts: a live
    # session's MCP server can hold the write lock for minutes (observed
    # 2026-09-04), so the 03:00 nightly indexes with a much longer wait
    # while interactive writers keep the snappier default.
    try:
        busy_ms = int(os.environ.get("SMRITI_DB_BUSY_TIMEOUT_MS", "10000"))
    except ValueError:
        busy_ms = 10_000
    busy_ms = max(busy_ms, 1000)
    conn = sqlite3.connect(str(db_path), timeout=max(busy_ms / 1000.0, 1.0))
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        return _ensure_schema_inner(conn, dimension)
    except BaseException:
        # Leak-proofing (2026-08-28 lock cascade): a throw mid-schema
        # left this connection's implicit write transaction OPEN in a
        # long-lived process, wedging every other writer — and their
        # own ensure_schema throws then leaked more open transactions,
        # a self-sustaining fleet-wide deadlock. Roll back and close on
        # any failure; never leak a connection holding a write lock.
        try:
            conn.rollback()
        finally:
            conn.close()
        raise


def _ensure_schema_inner(
    conn: sqlite3.Connection,
    dimension: int | None,
) -> IndexDB:

    has_vec = _load_sqlite_vec(conn)
    has_fts = _has_fts5(conn)

    # ── Main chunks table ────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id          TEXT PRIMARY KEY,
            source      TEXT NOT NULL,
            heading     TEXT DEFAULT '',
            heading_level INTEGER DEFAULT 0,
            content     TEXT NOT NULL,
            start_line  INTEGER,
            end_line    INTEGER,
            content_hash TEXT,
            trunk_distance INTEGER DEFAULT -1,
            indexed_at  TEXT NOT NULL
        )
    """)
    # Covering index for the two hot source-shaped queries: the write
    # path's delete-then-insert upsert (WHERE source = ?) and the
    # incremental indexer's mtime map (GROUP BY source, MAX(indexed_at)).
    # Without it both were full scans of a 74k-chunk table per write.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_source_indexed_at "
        "ON chunks (source, indexed_at)"
    )

    # ── Metadata table ───────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )

    # ── Sources: each indexed file as it was when it was read ────────
    # The incremental index skips a file only while its mtime and size
    # still match. (Comparing mtime with the time of indexing missed an
    # edit made while a run was embedding: its mtime predated the run.)
    # vectors = 0: the source's chunks are indexed for keyword search but
    # their vectors are pending (the embedder was unavailable); repairs fill
    # them in. Writers from before the column existed always embedded, so
    # its default, for their rows and theirs to come, is 1.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            source   TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size     INTEGER NOT NULL,
            vectors  INTEGER NOT NULL DEFAULT 1
        )
    """)
    _add_sources_vectors(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sources_pending ON sources (source) WHERE vectors = 0"
    )
    if dimension is not None:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('dimension', ?)",
            (str(dimension),),
        )

    # ── FTS5 ─────────────────────────────────────────────────────────
    if has_fts:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content, heading, source,
                content='chunks', content_rowid='rowid'
            )
        """)
        # Term document frequencies for search.fts_terms. A vocab table stores
        # nothing (it reads chunks_fts's index), and living in the main schema
        # lets query_only readers use it; they can't create a temp one.
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_vocab "
                "USING fts5vocab(chunks_fts, 'row')"
            )
        except sqlite3.OperationalError as exc:  # fts5vocab missing: fts_terms falls back
            log.debug("fts5vocab unavailable: %s", exc)
        # Sync triggers
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, content, heading, source)
                VALUES (new.rowid, new.content, new.heading, new.source);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, heading, source)
                VALUES ('delete', old.rowid, old.content, old.heading, old.source);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, heading, source)
                VALUES ('delete', old.rowid, old.content, old.heading, old.source);
                INSERT INTO chunks_fts(rowid, content, heading, source)
                VALUES (new.rowid, new.content, new.heading, new.source);
            END;
        """)
        log.info("FTS5 enabled")
    else:
        log.warning("FTS5 not available — keyword search disabled")

    # ── sqlite-vec ───────────────────────────────────────────────────
    if has_vec:
        size = dimension if dimension is not None else _recorded_dimension(conn)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                embedding float[{size}]
            )
        """)
        log.info("sqlite-vec enabled (dimension=%d)", size)
    else:
        log.warning("sqlite-vec not available — vector search disabled")

    conn.commit()

    return IndexDB(conn=conn, has_vec=has_vec, has_fts=has_fts)


def _add_sources_vectors(conn: sqlite3.Connection) -> None:
    """Migrate a schema-2 ``sources`` table: add ``vectors``, every existing source having them."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
    if "vectors" in columns:
        return
    try:
        conn.execute("ALTER TABLE sources ADD COLUMN vectors INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError as exc:
        # Another process migrated it between the check and the ALTER.
        if "duplicate column" not in str(exc).lower():
            raise


def _recorded_dimension(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    try:
        return int(row[0]) if row and row[0] else _PLACEHOLDER_DIM
    except ValueError:
        return _PLACEHOLDER_DIM


def pending_vectors(conn: IndexDB | sqlite3.Connection) -> int:
    """How many sources have their vectors pending (0 for an index from before the column)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM sources WHERE vectors = 0").fetchone()[0])
    except sqlite3.OperationalError:  # no sources table or no vectors column yet
        return 0

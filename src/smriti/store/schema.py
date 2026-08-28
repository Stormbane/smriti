"""Database schema for the smriti index.

Creates three linked structures:
- ``chunks`` table — main content with metadata
- ``chunks_fts`` FTS5 virtual table — keyword search
- ``chunks_vec`` vec0 virtual table — vector similarity search

The FTS5 and vec0 tables are linked to ``chunks`` by rowid.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class IndexDB:
    """Wrapper around a sqlite3 connection with capability flags."""

    conn: sqlite3.Connection
    has_vec: bool
    has_fts: bool

    def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self.conn.execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self.conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension.  Returns True on success."""
    try:
        import sqlite_vec  # type: ignore[import-untyped]

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:
        log.warning("sqlite-vec not available: %s", exc)
        return False


def _has_fts5(conn: sqlite3.Connection) -> bool:
    """Check whether the sqlite3 build includes FTS5."""
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
    dimension: int,
) -> sqlite3.Connection:
    """Open (or create) the index database and ensure all tables exist.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Parent directories are created
        automatically.
    dimension:
        Embedding vector dimension (set at schema creation time; changing it
        requires a full re-index).

    Returns
    -------
    IndexDB
        A wrapped connection with WAL mode, extensions loaded, and
        capability flags.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
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
    dimension: int,
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
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                embedding float[{dimension}]
            )
        """)
        log.info("sqlite-vec enabled (dimension=%d)", dimension)
    else:
        log.warning("sqlite-vec not available — vector search disabled")

    conn.commit()

    return IndexDB(conn=conn, has_vec=has_vec, has_fts=has_fts)

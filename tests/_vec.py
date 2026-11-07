"""Whether this Python can load sqlite-vec, for tests that need stored vectors.

Importing the package isn't enough: CI's macOS Python is built without
``enable_load_extension``, so the extension can't be loaded there and the
index is keyword-only.
"""

from __future__ import annotations

import sqlite3

import pytest


def can_load_vec() -> bool:
    try:
        import sqlite_vec
    except ImportError:
        return False
    conn = sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except (AttributeError, OSError, sqlite3.Error):
        return False
    finally:
        conn.close()


needs_vec = pytest.mark.skipif(not can_load_vec(),
                               reason="sqlite-vec can't be loaded by this Python")

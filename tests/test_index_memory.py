"""H12.14: a full index of about 100k chunks stays under a memory ceiling.

Slow (about two minutes): marked ``slow``, so excluded by default like
``tests/test_perf.py``; run it with ``pytest -m slow tests/test_index_memory.py``.

Before H12.14 ``index_tree`` held every chunk and every vector of a run at
once: on 20,000 stub chunks its Python allocations peaked at 270 MB (1.15 GB
working set), so about 1.35 GB at 100,000. Batched and staged on disk, they
peak at about 20 MB whatever the size of the tree. tracemalloc counts what
Python allocates (the chunks and vectors), not SQLite's own page cache,
which its cache size bounds.
"""

from __future__ import annotations

import sqlite3
import tracemalloc
from pathlib import Path

import pytest

from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.indexer import index_tree

pytestmark = pytest.mark.slow

FILES, SECTIONS = 1000, 100  # a chunk per section and one per title: 101,000 chunks
CEILING_MB = 64


def test_a_full_index_of_100k_chunks_stays_under_the_ceiling(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "tree"
    for f in range(FILES):
        folder = tree / f"d{f // 100}"
        folder.mkdir(parents=True, exist_ok=True)
        body = "".join(f"## Part {s}\n\nAri and Sam wrote part {s} of note {f}.\n\n"
                       for s in range(SECTIONS))
        (folder / f"note-{f}.md").write_text(f"# Note {f}\n\n{body}", encoding="utf-8")
    monkeypatch.setattr(embedding, "embedder", lambda: StubEmbedding())
    db = tmp_path / "index.db"

    tracemalloc.start()
    try:
        stats = index_tree(full=True, root=tree, db=db)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert stats["chunks"] >= 100_000 and stats["errors"] == 0
    assert peak < CEILING_MB * 1024 * 1024, f"peak {peak / 1e6:.0f} MB"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == stats["chunks"]
    finally:
        conn.close()

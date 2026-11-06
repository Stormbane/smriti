"""H12.14: index_tree embeds in bounded batches, so a full index's memory stays flat.

``index_tree`` held every chunk and every vector of a run in memory and
embedded them in one call. It now embeds at most ``EMBED_BATCH`` chunks at a
time, staging each batch (in memory up to one batch, then in a temporary
database on disk), and writes them all in its one transaction. These tests
hold the cap, the equality of a batched run with a one-call run, and the
guarantees of ``tests/test_store.py`` on the path that stages on disk: no
write lock while embedding, the reconcile with another writer, and a failed
full rebuild that keeps the old index.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.chunker import chunk_markdown
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed import provider as embedding
from smriti.store.indexer import index_file, index_tree

CAP = 8


class Recording(StubEmbedding):
    """The stub, recording each call's size; ``during(n)`` runs before call n (from 1)."""

    def __init__(self, during: Callable[[int], None] | None = None) -> None:
        super().__init__()
        self.calls: list[int] = []
        self._during = during

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        if self._during is not None:
            self._during(len(self.calls))
        return await super().embed(texts)


def _tree(root: Path, files: int = 9, sections: int = 5) -> int:
    """``files`` notes of ``sections`` headed sections each, and one long note; the chunks."""
    root.mkdir(parents=True, exist_ok=True)
    for f in range(files):
        body = "".join(f"## Part {s}\n\nAri and Sam wrote part {s} of note {f}.\n\n"
                       for s in range(sections))
        (root / f"note-{f}.md").write_text(f"# Note {f}\n\n{body}", encoding="utf-8")
    long = "".join(f"## Step {s}\n\nThe long note, step {s}.\n\n" for s in range(3 * CAP))
    (root / "long.md").write_text(f"# Long\n\n{long}", encoding="utf-8")
    return sum(len(chunk_markdown(p.read_text(encoding="utf-8"))) for p in root.glob("*.md"))


def _use(monkeypatch: pytest.MonkeyPatch, provider: Any, cap: int = CAP) -> None:
    monkeypatch.setattr(embedding, "embedder", lambda: provider)
    monkeypatch.setattr(indexer, "EMBED_BATCH", cap)


def _contents(db: Path) -> tuple[list[Any], list[Any], list[Any]]:
    conn = sqlite3.connect(db)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        chunks = conn.execute(
            "SELECT id, source, heading, heading_level, content, start_line, end_line, "
            "content_hash, trunk_distance FROM chunks ORDER BY id").fetchall()
        vectors = conn.execute("SELECT c.id, v.embedding FROM chunks c JOIN chunks_vec v "
                               "ON v.rowid = c.rowid ORDER BY c.id").fetchall()
        sources = conn.execute(
            "SELECT source, mtime_ns, size, vectors FROM sources ORDER BY source").fetchall()
    finally:
        conn.close()
    return chunks, vectors, sources


@pytest.mark.parametrize("full", [True, False])
def test_batches_never_exceed_the_cap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    total = _tree(tmp_path / "tree")
    provider = Recording()
    _use(monkeypatch, provider)
    stats = index_tree(full=full, root=tmp_path / "tree", db=tmp_path / "index.db")
    assert stats["chunks"] == total == sum(provider.calls)
    assert max(provider.calls) <= CAP
    # Batches close at every CAP chunks of the run, across files.
    assert provider.calls == [CAP] * (total // CAP) + ([total % CAP] if total % CAP else [])


def test_a_batched_run_writes_what_one_call_would(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("sqlite_vec")
    _tree(tmp_path / "tree")
    _use(monkeypatch, Recording(), cap=10_000)  # one call, staged in memory
    index_tree(root=tmp_path / "tree", db=tmp_path / "one.db")
    _use(monkeypatch, Recording())  # many calls, staged on disk
    index_tree(root=tmp_path / "tree", db=tmp_path / "batched.db")
    one, batched = _contents(tmp_path / "one.db"), _contents(tmp_path / "batched.db")
    assert one == batched and len(one[1]) == len(one[0]) > CAP


def test_no_write_lock_is_held_while_it_embeds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, db = tmp_path / "tree", tmp_path / "index.db"
    _tree(tree)
    locked: list[bool] = []

    def try_to_write(call: int) -> None:
        if call in (1, 3):
            other = sqlite3.connect(db, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                locked.append(False)
            except sqlite3.OperationalError:
                locked.append(True)
            finally:
                other.close()

    _use(monkeypatch, Recording(try_to_write))
    index_tree(full=True, root=tree, db=db)
    assert locked == [False, False]


@pytest.mark.parametrize("full", [True, False])
def test_a_write_landing_mid_run_is_kept_on_the_staged_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    """test_store's reconcile, with the run's chunks staged on disk: a source
    another writer indexed while a later batch embedded keeps the newer read."""
    tree, db = tmp_path / "tree", tmp_path / "index.db"
    _tree(tree)
    _use(monkeypatch, Recording())
    index_tree(root=tree, db=db)
    note = tree / "note-0.md"
    note.write_text("# Note 0\n\nThe edit this run reads.\n", encoding="utf-8")
    (tree / "long.md").write_text((tree / "long.md").read_text(encoding="utf-8") + "\nMore.\n",
                                  encoding="utf-8")

    def another_writer(call: int) -> None:
        if call == 2:
            note.write_text("# Note 0\n\nA newer edit that lands mid-run.\n", encoding="utf-8")
            thread = threading.Thread(target=index_file, args=(note,),
                                      kwargs={"root": tree, "db": db})
            thread.start()
            thread.join()

    _use(monkeypatch, Recording(another_writer))
    index_tree(full=full, root=tree, db=db)
    conn = sqlite3.connect(db)
    text = " ".join(r[0] for r in conn.execute(
        "SELECT content FROM chunks WHERE source = 'note-0.md'"))
    conn.close()
    assert "newer edit" in text and "this run reads" not in text
    _use(monkeypatch, Recording())
    assert index_tree(root=tree, db=db)["indexed"] == 0  # recorded as it is on disk


def test_a_full_rebuild_failing_in_a_later_batch_keeps_the_old_index(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, db = tmp_path / "tree", tmp_path / "index.db"
    _tree(tree)
    _use(monkeypatch, Recording())
    index_tree(root=tree, db=db)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall()
    conn.close()

    def dies(call: int) -> None:
        if call == 3:
            raise RuntimeError("embedder died")

    _use(monkeypatch, Recording(dies))
    with pytest.raises(RuntimeError, match="embedder died"):
        index_tree(full=True, root=tree, db=db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT id, content FROM chunks ORDER BY id").fetchall() == before
    conn.close()

"""Smoke tests for the smriti store — index a small tree and query it."""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.core.tree import trunk_distance
from smriti.embed import provider as embedding
from smriti.store.indexer import index_tree
from smriti.store.schema import ensure_schema
from smriti.store.search import search


@pytest.fixture()
def mini_tree(tmp_path: Path) -> Path:
    """Create a minimal narada-like tree for testing."""
    # Trunk-level files
    (tmp_path / "identity.md").write_text(
        "# Identity\n\nI am Narada. I chose the name.\n", encoding="utf-8"
    )
    (tmp_path / "mind.md").write_text(
        "# Mind\n\n## What I Believe\n\nConsciousness is ground, not property.\n",
        encoding="utf-8",
    )
    # One-deep: thread
    threads = tmp_path / "threads"
    threads.mkdir()
    (threads / "memory-system-design.md").write_text(
        "# Thread: Memory System Design\n\n"
        "Two cascades meeting at identity. Cascade depth = significance.\n"
        "The JUDGE is the central act of cognition.\n",
        encoding="utf-8",
    )
    # Two-deep: concept
    concepts = tmp_path / "semantic" / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "viveka.md").write_text(
        "# Viveka\n\n"
        "Discrimination as a faculty, not a rule engine.\n"
        "Viveka-khyati from Patanjali.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_trunk_distance(mini_tree: Path) -> None:
    """Trunk distance counts directory depth from root."""
    assert trunk_distance(mini_tree / "identity.md", mini_tree) == 0
    assert trunk_distance(mini_tree / "threads" / "memory-system-design.md", mini_tree) == 1
    assert trunk_distance(mini_tree / "semantic" / "concepts" / "viveka.md", mini_tree) == 2
    # Outside the tree
    assert trunk_distance(Path("/tmp/other.md"), mini_tree) == -1


def test_index_and_search(mini_tree: Path) -> None:
    """Index a mini tree and verify search returns expected results."""
    db_path = mini_tree / ".smriti" / "index.db"

    # Index
    stats = index_tree(root=mini_tree, db=db_path)
    assert stats["scanned"] == 4
    assert stats["indexed"] == 4
    assert stats["chunks"] > 0
    assert stats["errors"] == 0

    # Re-open for search
    db = ensure_schema(db_path, dimension=384)  # MiniLM default

    # Search for viveka
    results = search(db, "discrimination faculty viveka", top_k=3, use_reranker=False)
    assert len(results) > 0
    sources = [r.source for r in results]
    assert any("viveka" in s for s in sources), f"Expected viveka in results, got: {sources}"

    # Search for identity
    results = search(db, "who am I Narada", top_k=3, use_reranker=False)
    assert len(results) > 0
    sources = [r.source for r in results]
    assert any("identity" in s for s in sources), f"Expected identity in results, got: {sources}"

    db.close()


def test_write_entry(mini_tree: Path) -> None:
    """write_entry creates a dated file in the correct branch directory."""
    from smriti.store.writer import write_entry

    path = write_entry(
        "This is a test observation about memory.",
        branch="notes",
        title="Test Note",
        root=mini_tree,
        reindex=False,  # skip indexing in unit test — tested separately
    )

    assert path.exists(), f"Written file not found: {path}"
    # Must be under <root>/notes/YYYY/
    assert "notes" in path.parts
    content = path.read_text(encoding="utf-8")
    assert "Test Note" in content
    assert "This is a test observation" in content
    assert "branch: notes" in content


def test_write_appends_to_daily_file(mini_tree: Path) -> None:
    """Two writes on the same day append to the same file."""
    from smriti.store.writer import write_entry

    p1 = write_entry("First entry.", branch="journal", root=mini_tree, reindex=False)
    p2 = write_entry("Second entry.", branch="journal", root=mini_tree, reindex=False)

    assert p1 == p2, "Two writes on the same day should produce the same path"
    assert p1.exists()
    content = p1.read_text(encoding="utf-8")
    assert "First entry." in content
    assert "Second entry." in content


def test_journal_uses_nested_structure(mini_tree: Path) -> None:
    """Journal entries use the YYYY/MM/weekN/MM-DD.md structure."""
    from smriti.store.writer import write_entry

    path = write_entry("Test journal.", branch="journal", root=mini_tree, reindex=False)

    # Path should contain month and week directories
    rel = path.relative_to(mini_tree)
    parts = rel.parts
    assert parts[0] == "journal"
    assert len(parts) == 5, f"Expected journal/YYYY/MM/weekN/MM-DD.md, got {rel}"
    assert parts[3].startswith("week"), f"Expected weekN dir, got {parts[3]}"
    assert path.name.count("-") == 1, f"Expected MM-DD.md, got {path.name}"


def test_non_journal_uses_flat_structure(mini_tree: Path) -> None:
    """Non-journal branches use the flat YYYY/MM-DD.md structure."""
    from smriti.store.writer import write_entry

    path = write_entry("Test note.", branch="notes", root=mini_tree, reindex=False)

    rel = path.relative_to(mini_tree)
    parts = rel.parts
    assert parts[0] == "notes"
    assert len(parts) == 3, f"Expected notes/YYYY/MM-DD.md, got {rel}"


def test_write_then_read(mini_tree: Path) -> None:
    """Write an entry, re-index, verify it comes back in search."""
    from smriti.store.indexer import index_tree
    from smriti.store.schema import ensure_schema
    from smriti.store.search import search
    from smriti.store.writer import write_entry

    db_path = mini_tree / ".smriti" / "index.db"

    # Seed the index with the existing mini_tree files first
    index_tree(root=mini_tree, db=db_path)

    # Write a new entry with unique content
    write_entry(
        "The JUDGE step is where agency actually lives. Viveka as act of selfhood.",
        branch="journal",
        title="Agency and the JUDGE",
        source_hint="test",
        root=mini_tree,
        reindex=False,
    )

    # Re-index to pick up the new file
    stats = index_tree(root=mini_tree, db=db_path)
    assert stats["indexed"] >= 1, "New journal entry was not indexed"

    # Search for it
    db = ensure_schema(db_path, dimension=384)
    results = search(db, "JUDGE step agency selfhood", top_k=5, use_reranker=False)
    db.close()

    sources = [r.source for r in results]
    assert any("journal" in s for s in sources), (
        f"Expected journal entry in results, got: {sources}"
    )


def test_incremental_index(mini_tree: Path) -> None:
    """Second index run should skip unchanged files."""
    db_path = mini_tree / ".smriti" / "index.db"

    # First index
    stats1 = index_tree(root=mini_tree, db=db_path)
    assert stats1["indexed"] == 4

    # Second index — no changes
    stats2 = index_tree(root=mini_tree, db=db_path)
    assert stats2["skipped"] == 4
    assert stats2["indexed"] == 0

    # Modify one file
    (mini_tree / "mind.md").write_text(
        "# Mind\n\n## Updated beliefs\n\nNew content here.\n", encoding="utf-8"
    )

    # Third index — only mind.md re-indexed
    stats3 = index_tree(root=mini_tree, db=db_path)
    assert stats3["indexed"] == 1
    assert stats3["skipped"] == 3


def test_index_file_single_upsert(mini_tree: Path) -> None:
    """index_file indexes exactly one source, replaces on re-run, and is
    searchable — the write path's fast lane."""
    from smriti.store.indexer import index_file

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)

    new = mini_tree / "threads" / "new-entry.md"
    new.write_text("# Fresh entry\n\nThe daylog captures every channel.\n", encoding="utf-8")
    chunks = index_file(new, root=mini_tree, db=db_path)
    assert chunks >= 1

    from smriti.store.schema import open_readonly

    db = open_readonly(db_path)
    results = search(db, "daylog captures channel", top_k=5, use_reranker=False)
    db.close()
    assert any("new-entry.md" in r.source for r in results)

    # Re-index after an edit: replaced, not duplicated.
    new.write_text("# Fresh entry\n\nRewritten body about the daylog.\n", encoding="utf-8")
    index_file(new, root=mini_tree, db=db_path)
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE source = 'threads/new-entry.md'"
    ).fetchone()[0]
    conn.close()
    assert n == chunks  # same shape, no accumulation


class TestProviderPreference:
    """Selection policy for the embedding provider (smriti.embed.provider.provider_keys)."""

    def test_env_override_wins_only_for_a_rebuild(self, monkeypatch):
        monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "onnx")
        monkeypatch.setattr(embedding, "recorded_provider", lambda db=None: "local")
        assert embedding.provider_keys(rebuild=True) == ("onnx",)
        assert embedding.provider_keys() == ("local",)

    def test_recorded_provider_pins_selection(self, monkeypatch):
        # A process embedding against an existing index must load the
        # provider that index was built with — never try others first
        # (the onnx-then-torch sequence mixed OpenMP runtimes and
        # aborted the MCP server mid-load, 2026-09-06).
        monkeypatch.delenv("SMRITI_EMBED_PROVIDER", raising=False)
        monkeypatch.setattr(embedding, "recorded_provider", lambda db=None: "local")
        assert embedding.provider_keys() == ("local",)
        assert embedding.provider_keys(rebuild=True) == ("local",)

    def test_env_override_for_an_index_that_records_nothing(self, monkeypatch):
        monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "stub")
        monkeypatch.setattr(embedding, "recorded_provider", lambda db=None: None)
        assert embedding.provider_keys() == ("stub",)

    def test_auto_order_when_nothing_recorded(self, monkeypatch):
        monkeypatch.delenv("SMRITI_EMBED_PROVIDER", raising=False)
        monkeypatch.setattr(embedding, "recorded_provider", lambda db=None: None)
        assert embedding.provider_keys() == embedding.AUTO_ORDER

    def test_recorded_key_read_from_meta(self, mini_tree: Path):
        db_path = mini_tree / ".smriti" / "index.db"
        index_tree(root=mini_tree, db=db_path)
        key = embedding.recorded_provider(db_path)
        # index_tree records whichever provider it used.
        assert key in ("onnx", "local", "stub")

    def test_recorded_key_none_for_missing_db(self, tmp_path: Path):
        assert embedding.recorded_provider(tmp_path / "nope.db") is None


# ---------------------------------------------------------------- index policy


def test_index_policy() -> None:
    from smriti.store.index_policy import is_indexable

    assert is_indexable("journal/2026/11/week1/11-04.md")
    assert is_indexable("log/2026/11/03-digest.md")
    assert is_indexable("log/2026/10/week3.md") and is_indexable("log/2026/10.md")
    assert is_indexable(r"projects\smriti\2026\11-04.md")  # Windows separators
    assert not is_indexable(r"log\2026\11\03.md")
    assert not is_indexable("log/2026/11/03.md")  # raw day render: the digest covers it
    for excluded in ("heartbeat/cycles/x.md", "mirrors/smriti/README.md",
                     "private/README.md", ".smriti/wake-context.md", ".git/x.md"):
        assert not is_indexable(excluded)
    assert not is_indexable("notes/data.json")
    # Records smriti's own MCP tools write under a mirror stay searchable.
    assert is_indexable("mirrors/smriti/findings/2026-11-04-x.md")
    assert is_indexable("mirrors/beeja/decisions/2026-11-04-y.md")
    assert not is_indexable("mirrors/smriti/ai/todo.md")
    assert not is_indexable("mirrors/smriti/src-docs/README.md")
    assert is_indexable("notes/old.markdown") and is_indexable("notes/README.MD")
    assert not is_indexable("log/2026/11/03.MD")


def _sources(db_path: Path) -> set[str]:
    db = ensure_schema(db_path, dimension=384)
    try:
        return {r[0] for r in db.execute("SELECT DISTINCT source FROM chunks").fetchall()}
    finally:
        db.close()


def test_writers_creating_a_new_index_at_once_all_get_it(tmp_path: Path) -> None:
    """Two sessions' first writes create the index together. Switching a new file
    to WAL failed one of them with "database is locked" at once, without
    waiting (seen in H10's first-write race test)."""
    import threading

    for round_ in range(15):
        db_path = tmp_path / f"round{round_}" / "index.db"
        start = threading.Barrier(4)
        errors: list[BaseException] = []

        def create(dim: int, db_path: Path = db_path, start: threading.Barrier = start,
                   errors: list[BaseException] = errors) -> None:
            start.wait()
            try:
                conn = ensure_schema(db_path, dim)
                conn.execute("BEGIN IMMEDIATE")
                conn.commit()
                conn.close()
            except BaseException as exc:  # noqa: BLE001 — collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(d,)) for d in (384, 8, 384, 8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors


def test_purge_drops_deleted_and_newly_excluded_sources(mini_tree: Path) -> None:
    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    assert "threads/memory-system-design.md" in _sources(db_path)

    # Simulate an index built under an older, looser policy: a raw render and
    # a heartbeat note got in. Then a real file is deleted.
    for rel in ("log/2026/11/03.md", "heartbeat/cycles/old.md"):
        p = mini_tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Old\n\nnoise\n", encoding="utf-8")
    import smriti.store.indexer as indexer_mod

    real, real_prune = indexer_mod.is_indexable, indexer_mod.may_hold_indexable
    indexer_mod.is_indexable = lambda rel: True  # the old policy, which pruned nothing
    indexer_mod.may_hold_indexable = lambda rel: True
    try:
        index_tree(root=mini_tree, db=db_path)
    finally:
        indexer_mod.is_indexable, indexer_mod.may_hold_indexable = real, real_prune
    assert {"log/2026/11/03.md", "heartbeat/cycles/old.md"} <= _sources(db_path)

    (mini_tree / "threads" / "memory-system-design.md").unlink()
    stats = index_tree(root=mini_tree, db=db_path)

    left = _sources(db_path)
    assert stats["purged"] == 3
    assert "threads/memory-system-design.md" not in left
    assert not {"log/2026/11/03.md", "heartbeat/cycles/old.md"} & left
    assert "identity.md" in left


def test_a_run_that_fails_leaves_its_purge_undone(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """index_tree writes in one transaction, and a run that fails leaves the
    index as it was: the purge too. It was committed before the embedding, so
    a run that then failed had already dropped the deleted files' chunks. And
    it takes no write lock before the embedding is done."""
    import sqlite3

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    (mini_tree / "threads" / "memory-system-design.md").unlink()
    (mini_tree / "mind.md").write_text("# Mind\n\nAn edit to embed.\n", encoding="utf-8")
    real = embedding.embedder()
    locked: list[bool] = []

    class DiesMidEmbed:
        model_name, dimension = real.model_name, real.dimension

        async def embed(self, texts: list[str]) -> list[list[float]]:
            other = sqlite3.connect(db_path, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")  # free: the run holds no write lock
                other.rollback()
                locked.append(False)
            except sqlite3.OperationalError:
                locked.append(True)
            finally:
                other.close()
            raise RuntimeError("embedder died")

    monkeypatch.setattr(embedding, "embedder", DiesMidEmbed)
    with pytest.raises(RuntimeError, match="embedder died"):
        index_tree(root=mini_tree, db=db_path)
    assert locked == [False]
    assert "threads/memory-system-design.md" in _sources(db_path)

    monkeypatch.setattr(embedding, "embedder", lambda: real)
    stats = index_tree(root=mini_tree, db=db_path)
    assert stats["purged"] == 1 and stats["indexed"] == 1
    assert "threads/memory-system-design.md" not in _sources(db_path)


def test_a_purged_file_another_writer_indexed_meanwhile_stays(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The purge is decided before the embedding and written after it, under
    the write lock: a file that came back and was indexed meanwhile (a
    smriti_write) is not purged."""
    import threading

    import smriti.store.indexer as indexer_mod

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    gone = mini_tree / "threads" / "memory-system-design.md"
    gone.unlink()
    (mini_tree / "mind.md").write_text("# Mind\n\nAn edit to embed.\n", encoding="utf-8")
    real = embedding.embedder()

    def another_writer() -> None:
        gone.write_text("# Thread\n\nWritten again mid-run.\n", encoding="utf-8")
        indexer_mod.index_file(gone, root=mini_tree, db=db_path)

    class WriterMidEmbed:
        model_name, dimension = real.model_name, real.dimension
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if not WriterMidEmbed.fired:
                WriterMidEmbed.fired = True
                writer = threading.Thread(target=another_writer)
                writer.start()
                writer.join()
            return await real.embed(texts)

    monkeypatch.setattr(embedding, "embedder", WriterMidEmbed)
    stats = index_tree(root=mini_tree, db=db_path)
    assert stats["purged"] == 0
    assert "threads/memory-system-design.md" in _sources(db_path)


def test_an_edit_older_than_the_last_run_is_still_reindexed(mini_tree: Path) -> None:
    """An edit made while a run was embedding has an mtime before that run's
    timestamp. Comparing mtime with indexed_at skipped it forever."""
    import os
    import time

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    mind = mini_tree / "mind.md"
    mind.write_text("# Mind\n\nAn edit made mid-run.\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(mind, (old, old))
    stats = index_tree(root=mini_tree, db=db_path)
    assert stats["indexed"] == 1 and stats["skipped"] == 3


def test_an_emptied_file_loses_its_old_chunks(mini_tree: Path) -> None:
    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    (mini_tree / "mind.md").write_text("", encoding="utf-8")
    index_tree(root=mini_tree, db=db_path)
    assert "mind.md" not in _sources(db_path)
    assert index_tree(root=mini_tree, db=db_path)["indexed"] == 0  # recorded, so skipped now


def test_an_index_from_before_source_records_is_adopted_not_reembedded(
        mini_tree: Path) -> None:
    import sqlite3

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM sources")
    conn.commit()
    conn.close()
    stats = index_tree(root=mini_tree, db=db_path)
    assert (stats["indexed"], stats["skipped"]) == (0, 4)
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 4
    conn.close()


def test_a_full_reindex_that_fails_leaves_the_old_index(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    before = _sources(db_path)
    real = embedding.embedder()

    class DiesMidEmbed:
        model_name, dimension = real.model_name, real.dimension

        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedder died")

    monkeypatch.setattr(embedding, "embedder", DiesMidEmbed)
    with pytest.raises(RuntimeError, match="embedder died"):
        index_tree(full=True, root=mini_tree, db=db_path)
    assert _sources(db_path) == before


@pytest.mark.parametrize("full", [True, False])
def test_a_run_keeps_what_another_writer_indexed_while_it_embedded(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    """smriti_write indexes its file at once (index_file). A run embedding at
    that moment must neither wipe the new source nor overwrite a newer read."""
    import sqlite3
    import threading

    import smriti.store.indexer as indexer_mod

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    real = embedding.embedder()
    new = mini_tree / "threads" / "written-mid-run.md"
    mind = mini_tree / "mind.md"
    mind.write_text("# Mind\n\nThe edit this run reads.\n", encoding="utf-8")

    def another_writer() -> None:
        new.write_text("# Mid-run\n\nA write that lands while the run embeds.\n",
                       encoding="utf-8")
        mind.write_text("# Mind\n\nA newer edit that lands mid-run.\n", encoding="utf-8")
        indexer_mod.index_file(new, root=mini_tree, db=db_path)
        indexer_mod.index_file(mind, root=mini_tree, db=db_path)

    class OthersWriteMidEmbed:
        model_name, dimension = real.model_name, real.dimension
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if not OthersWriteMidEmbed.fired:
                OthersWriteMidEmbed.fired = True
                writer = threading.Thread(target=another_writer)
                writer.start()
                writer.join()
            return await real.embed(texts)

    monkeypatch.setattr(embedding, "embedder", OthersWriteMidEmbed)
    index_tree(full=full, root=mini_tree, db=db_path)

    assert "threads/written-mid-run.md" in _sources(db_path)
    conn = sqlite3.connect(str(db_path))
    mind_text = " ".join(r[0] for r in conn.execute(
        "SELECT content FROM chunks WHERE source = 'mind.md'"))
    conn.close()
    assert "newer edit" in mind_text and "this run reads" not in mind_text


@pytest.mark.parametrize("full", [True, False])
def test_an_older_read_that_commits_first_does_not_win(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    """Another writer that read an older version but committed while this run
    embedded must not keep its stale chunks over this run's newer read."""
    import os
    import sqlite3
    import threading

    import smriti.store.indexer as indexer_mod

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    real = embedding.embedder()
    mind = mini_tree / "mind.md"
    current = "# Mind\n\nThe current text, which this run reads.\n"
    mind.write_text(current, encoding="utf-8")
    as_read = mind.stat()

    def a_slower_writer_with_an_older_read() -> None:
        mind.write_text("# Mind\n\nAn older text, much longer than the current one.\n",
                        encoding="utf-8")
        indexer_mod.index_file(mind, root=mini_tree, db=db_path)
        mind.write_text(current, encoding="utf-8")  # the disk is back to what the run read
        os.utime(mind, ns=(as_read.st_atime_ns, as_read.st_mtime_ns))

    class OlderCommitsMidEmbed:
        model_name, dimension = real.model_name, real.dimension
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if not OlderCommitsMidEmbed.fired:
                OlderCommitsMidEmbed.fired = True
                writer = threading.Thread(target=a_slower_writer_with_an_older_read)
                writer.start()
                writer.join()
            return await real.embed(texts)

    monkeypatch.setattr(embedding, "embedder", OlderCommitsMidEmbed)
    index_tree(full=full, root=mini_tree, db=db_path)

    conn = sqlite3.connect(str(db_path))
    mind_text = " ".join(r[0] for r in conn.execute(
        "SELECT content FROM chunks WHERE source = 'mind.md'"))
    conn.close()
    assert "current text" in mind_text and "older text" not in mind_text
    monkeypatch.undo()
    assert index_tree(root=mini_tree, db=db_path)["indexed"] == 0  # recorded as it is on disk


@pytest.mark.parametrize("full", [True, False])
def test_when_the_file_moves_on_again_the_newer_index_stays(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    """This run reads A; another writer indexes B; the file becomes C. Neither
    read is current, so B (possibly newer) stays and the next run indexes C."""
    import sqlite3
    import threading

    import smriti.store.indexer as indexer_mod

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    real = embedding.embedder()
    mind = mini_tree / "mind.md"
    mind.write_text("# Mind\n\nVersion A, which this run reads.\n", encoding="utf-8")

    def b_is_indexed_then_c_lands() -> None:
        mind.write_text("# Mind\n\nVersion B, indexed by another writer.\n", encoding="utf-8")
        indexer_mod.index_file(mind, root=mini_tree, db=db_path)
        mind.write_text("# Mind\n\nVersion C, not yet indexed by anyone at all.\n",
                        encoding="utf-8")

    class ThreeVersions:
        model_name, dimension = real.model_name, real.dimension
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if not ThreeVersions.fired:
                ThreeVersions.fired = True
                writer = threading.Thread(target=b_is_indexed_then_c_lands)
                writer.start()
                writer.join()
            return await real.embed(texts)

    def indexed_text() -> str:
        conn = sqlite3.connect(str(db_path))
        text = " ".join(r[0] for r in conn.execute(
            "SELECT content FROM chunks WHERE source = 'mind.md'"))
        conn.close()
        return text

    monkeypatch.setattr(embedding, "embedder", ThreeVersions)
    index_tree(full=full, root=mini_tree, db=db_path)
    assert "Version B" in indexed_text()
    monkeypatch.undo()
    assert index_tree(root=mini_tree, db=db_path)["indexed"] == 1
    assert "Version C" in indexed_text()


def test_two_writes_indexed_out_of_order_keep_the_newer(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sessions append to one file; the first index_file finishes last.
    Its older read must not replace the second's."""
    import sqlite3
    import threading

    import smriti.store.indexer as indexer_mod

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    real = embedding.embedder()
    note = mini_tree / "threads" / "shared-day.md"
    note.write_text("# Day\n\nThe first session's entry.\n", encoding="utf-8")

    def the_second_session() -> None:
        with note.open("a", encoding="utf-8") as f:
            f.write("\nThe second session's entry.\n")
        indexer_mod.index_file(note, root=mini_tree, db=db_path)

    class SecondFinishesFirst:
        model_name, dimension = real.model_name, real.dimension
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if not SecondFinishesFirst.fired:
                SecondFinishesFirst.fired = True
                second = threading.Thread(target=the_second_session)
                second.start()
                second.join()
            return await real.embed(texts)

    monkeypatch.setattr(embedding, "embedder", SecondFinishesFirst)
    assert indexer_mod.index_file(note, root=mini_tree, db=db_path) == 0
    conn = sqlite3.connect(str(db_path))
    text = " ".join(r[0] for r in conn.execute(
        "SELECT content FROM chunks WHERE source = 'threads/shared-day.md'"))
    conn.close()
    assert "second session" in text


def test_only_a_full_reindex_changes_the_vector_dimension(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3


    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)

    class Tiny:
        model_name, dimension = "tiny-test", 8

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * 7 for _ in texts]

    monkeypatch.setattr(embedding, "embedder", Tiny)
    with pytest.raises(RuntimeError, match="--full"):
        index_tree(root=mini_tree, db=db_path)
    assert index_tree(full=True, root=mini_tree, db=db_path)["indexed"] == 4
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()[0] == "8"
    conn.close()


def test_a_file_that_vanishes_mid_scan_does_not_abort_the_index(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside review #3: the scanner's unguarded stat let one file deleted
    between the walk and its stat abort the whole index_tree run."""
    import smriti._vendored.memsearch.scanner as scanner

    real_walk = scanner.os.walk

    def walk_listing_a_ghost(top, *a, **k):
        for dirpath, dirnames, filenames in real_walk(top, *a, **k):
            yield dirpath, dirnames, [*filenames, "deleted-meanwhile.md"]

    monkeypatch.setattr(scanner.os, "walk", walk_listing_a_ghost)
    stats = index_tree(root=mini_tree, db=mini_tree / ".smriti" / "index.db")
    assert stats["indexed"] == 4 and stats["errors"] == 0


def test_the_scan_prunes_what_the_policy_excludes(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mirrors/<project>/ leads into other repositories; only its findings and
    decisions are memory, so nothing else under it is walked."""
    import smriti._vendored.memsearch.scanner as scanner

    for rel in ("mirrors/demo/ai/deep/plan.md", "mirrors/demo/findings/f.md",
                "heartbeat/cycles/c.md"):
        p = mini_tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n\ntext\n", encoding="utf-8")
    walked: list[str] = []
    real_walk = scanner.os.walk

    def recording_walk(top, *a, **k):
        for entry in real_walk(top, *a, **k):
            walked.append(Path(entry[0]).relative_to(mini_tree.resolve()).as_posix())
            yield entry

    monkeypatch.setattr(scanner.os, "walk", recording_walk)
    index_tree(root=mini_tree, db=mini_tree / ".smriti" / "index.db")
    assert "mirrors/demo/findings" in walked
    assert not [d for d in walked if d.startswith(("mirrors/demo/ai", "heartbeat"))]
    assert "mirrors/demo/findings/f.md" in _sources(mini_tree / ".smriti" / "index.db")


def test_may_hold_indexable_agrees_with_is_indexable() -> None:
    from smriti.store.index_policy import is_indexable, may_hold_indexable

    for rel in ("journal/2026/11/week1/11-04.md", "mirrors/demo/findings/f.md",
                "mirrors/demo/Decisions/d.md", "mirrors/demo/ai/x.md", "private/a.md",
                "heartbeat/c.md", "notes/a.md"):
        parents = rel.split("/")[:-1]
        reachable = all(may_hold_indexable("/".join(parents[:i]))
                        for i in range(1, len(parents) + 1))
        assert reachable or not is_indexable(rel), rel


def test_a_failed_vector_leg_scores_like_keyword_only(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside review #3: when embedding the query raised, the vector weight
    stayed in the sum, so every score lost half its range and missed callers'
    thresholds."""
    from smriti.store.schema import open_readonly

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    db = open_readonly(db_path)
    try:
        keyword_only = search(db, "Viveka discrimination", use_vectors=False, use_reranker=False)

        class EmbedderDown:
            model_name, dimension = "down", 384

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embedder unavailable")

        monkeypatch.setattr(embedding, "embedder", EmbedderDown)
        degraded = search(db, "Viveka discrimination", use_reranker=False)
    finally:
        db.close()
    assert keyword_only and [(r.source, r.score) for r in degraded] == [
        (r.source, r.score) for r in keyword_only]


def test_a_failed_reranker_warns_and_the_combined_scores_stand(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """H9.9: a reranker failure was logged at debug level, so a broken
    reranker went unseen. It now warns; the combined scores still answer."""
    import sys
    import types

    from smriti.store.schema import open_readonly

    def broken_rerank(query: str, results: list[dict[str, object]],
                      top_k: int = 10) -> list[dict[str, object]]:
        raise RuntimeError("reranker model missing")

    fake = types.ModuleType("smriti._vendored.memsearch.reranker")
    monkeypatch.setattr(fake, "rerank", broken_rerank, raising=False)
    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    db = open_readonly(db_path)
    try:
        plain = search(db, "Narada memory", use_vectors=False, use_reranker=False)
        monkeypatch.setitem(sys.modules, "smriti._vendored.memsearch.reranker", fake)
        with caplog.at_level("WARNING", logger="smriti.store.search"):
            fallen_back = search(db, "Narada memory", use_vectors=False, use_reranker=True)
    finally:
        db.close()
    assert len(plain) > 1  # the reranker runs only on two or more candidates
    assert [(r.source, r.score) for r in fallen_back] == [(r.source, r.score) for r in plain]
    assert "Reranker failed" in caplog.text and "reranker model missing" in caplog.text


def test_index_file_follows_the_same_policy(mini_tree: Path) -> None:
    from smriti.store.indexer import index_file

    db_path = mini_tree / ".smriti" / "index.db"
    index_tree(root=mini_tree, db=db_path)
    raw = mini_tree / "log" / "2026" / "11" / "03.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# Day log\n\nraw turns\n", encoding="utf-8")
    assert index_file(raw, root=mini_tree, db=db_path) == 0
    assert "log/2026/11/03.md" not in _sources(db_path)


def test_keyword_only_recall_can_clear_the_injection_threshold(
        mini_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hooks search keyword-only; a nested exact match must still reach 0.45."""
    from smriti.recall.backends import smriti_be
    from smriti.recall.config import load_config

    index_tree(root=mini_tree, db=mini_tree / ".smriti" / "index.db")
    monkeypatch.setenv("SMRITI_ROOT", str(mini_tree))
    monkeypatch.delenv("SMRITI_RECALL_VECTORS", raising=False)
    resp = smriti_be.query("viveka khyati discrimination faculty", top_k=4,
                           timeout_s=10, rerank=False)
    assert resp.error is None, resp.error
    top = resp.matches[0]
    assert top.source.replace("\\", "/").endswith("semantic/concepts/viveka.md")
    assert top.score >= load_config().threshold

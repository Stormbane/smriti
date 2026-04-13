"""Smoke tests for the smriti store — index a small tree and query it."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from smriti.core.tree import trunk_distance
from smriti.store.indexer import index_tree
from smriti.store.schema import ensure_schema
from smriti.store.search import SearchResult, search


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


def test_write_increments_counter(mini_tree: Path) -> None:
    """Two writes on the same day get different counters."""
    from smriti.store.writer import write_entry

    p1 = write_entry("First entry.", branch="journal", root=mini_tree, reindex=False)
    p2 = write_entry("Second entry.", branch="journal", root=mini_tree, reindex=False)

    assert p1 != p2, "Two writes produced the same path"
    assert p1.exists()
    assert p2.exists()


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

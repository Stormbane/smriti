"""Tests for the structural cascade's index.md maintenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.store.cascade import structural_cascade


@pytest.fixture()
def cascade_tree(tmp_path: Path) -> Path:
    """Create a tree with index.md files for cascade testing."""
    # Root
    (tmp_path / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
    (tmp_path / "identity.md").write_text("# Identity\n", encoding="utf-8")

    # Threads with index
    threads = tmp_path / "threads"
    threads.mkdir()
    (threads / "index.md").write_text(
        "# Threads\n\n- [[threads/memory-design]] — memory system\n",
        encoding="utf-8",
    )
    (threads / "memory-design.md").write_text(
        "# Memory Design\n\nReferences [[semantic/concepts/viveka]].\n",
        encoding="utf-8",
    )

    # Concepts with index
    concepts = tmp_path / "semantic" / "concepts"
    concepts.mkdir(parents=True)
    (concepts / "index.md").write_text(
        "# Concepts\n\n- [[semantic/concepts/viveka]] — discrimination\n",
        encoding="utf-8",
    )
    (concepts / "viveka.md").write_text(
        "# Viveka\n\nDiscrimination as a faculty.\n",
        encoding="utf-8",
    )

    # .smriti directory (should be ignored)
    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()

    return tmp_path


def test_structural_cascade_updates_index(cascade_tree: Path) -> None:
    """Adding a file triggers parent index.md update."""
    concepts = cascade_tree / "semantic" / "concepts"
    new_file = concepts / "sovereignty.md"
    new_file.write_text("# Sovereignty\n\nLoRA always-on.\n", encoding="utf-8")

    updated = structural_cascade(new_file, cascade_tree)

    assert len(updated) >= 1
    # The concepts/index.md should have been updated
    index_content = (concepts / "index.md").read_text(encoding="utf-8")
    assert "sovereignty" in index_content.lower()


def test_structural_cascade_stops_at_root(cascade_tree: Path) -> None:
    """Cascade does not touch MEMORY.md at root."""
    concepts = cascade_tree / "semantic" / "concepts"
    new_file = concepts / "test.md"
    new_file.write_text("# Test\n", encoding="utf-8")

    updated = structural_cascade(new_file, cascade_tree)

    updated_names = [p.name for p in updated]
    assert "MEMORY.md" not in updated_names

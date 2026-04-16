"""Tests for the watcher module's event handling logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from smriti.watcher import _on_change


@pytest.fixture()
def watch_tree(tmp_path: Path) -> Path:
    """Create a minimal tree for watcher tests."""
    (tmp_path / "concepts").mkdir()
    (tmp_path / "concepts" / "viveka.md").write_text("# Viveka\n", encoding="utf-8")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "2026").mkdir()
    (tmp_path / ".smriti").mkdir()
    (tmp_path / ".smriti" / "index.db").write_text("", encoding="utf-8")
    return tmp_path


def test_on_change_skips_smriti_dir(watch_tree: Path):
    """Files under .smriti/ should be silently ignored."""
    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade") as mock_cascade, \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("created", watch_tree / ".smriti" / "index.db")
        mock_cascade.assert_not_called()
        mock_enqueue.assert_not_called()


def test_on_change_skips_index_md(watch_tree: Path):
    """index.md is written by structural cascade — watching it would loop."""
    index = watch_tree / "concepts" / "index.md"
    index.write_text("# Index\n", encoding="utf-8")

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade") as mock_cascade, \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("modified", index)
        mock_cascade.assert_not_called()
        mock_enqueue.assert_not_called()


def test_on_change_skips_non_markdown(watch_tree: Path):
    """Non-markdown files are ignored."""
    txt = watch_tree / "notes.txt"
    txt.write_text("hello\n", encoding="utf-8")

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade") as mock_cascade, \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("created", txt)
        mock_cascade.assert_not_called()
        mock_enqueue.assert_not_called()


def test_on_change_non_leaf_queues_route(watch_tree: Path):
    """Non-leaf .md files queue a 'route' task."""
    concept = watch_tree / "concepts" / "new-idea.md"
    concept.write_text("# New Idea\n", encoding="utf-8")

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade", return_value=[]), \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("created", concept)
        mock_enqueue.assert_called_once()
        task = mock_enqueue.call_args[0][0]
        assert task.type == "route"
        assert "concepts/new-idea.md" in task.path


def test_on_change_leaf_queues_ingest(watch_tree: Path):
    """Leaf .md files (inbox/, sources/, etc.) queue an 'ingest' task."""
    inbox_file = watch_tree / "inbox" / "paper.md"
    inbox_file.write_text("# Paper\n", encoding="utf-8")

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade", return_value=[]), \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("created", inbox_file)
        mock_enqueue.assert_called_once()
        task = mock_enqueue.call_args[0][0]
        assert task.type == "ingest"
        assert "inbox/paper.md" in task.path


def test_on_change_sources_queues_ingest(watch_tree: Path):
    """Files under sources/ are leaf — queue ingest."""
    src = watch_tree / "sources" / "2026" / "04-16-001.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Source\n", encoding="utf-8")

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade", return_value=[]), \
         patch("smriti.watcher.enqueue") as mock_enqueue:

        _on_change("modified", src)
        mock_enqueue.assert_called_once()
        task = mock_enqueue.call_args[0][0]
        assert task.type == "ingest"


def test_on_change_always_runs_structural_cascade(watch_tree: Path):
    """Structural cascade runs on every valid change regardless of leaf/non-leaf."""
    concept = watch_tree / "concepts" / "viveka.md"

    with patch("smriti.watcher.tree_root", return_value=watch_tree), \
         patch("smriti.watcher.structural_cascade", return_value=[]) as mock_cascade, \
         patch("smriti.watcher.enqueue"):

        _on_change("modified", concept)
        mock_cascade.assert_called_once_with(concept, watch_tree)

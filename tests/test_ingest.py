"""Tests for the ingest pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.store.ingest import IngestResult, _read_source, ingest
from smriti.store.router import RoutingAction, RoutingResult


# ── Source reading ──────────────────────────────────────────────────


def test_read_source_file(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text("# Hello\n\nWorld.\n", encoding="utf-8")

    content, source_type = _read_source(str(f))
    assert source_type == "file"
    assert "Hello" in content
    assert "World" in content


def test_read_source_directory(tmp_path: Path):
    (tmp_path / "a.md").write_text("# File A\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("File B content\n", encoding="utf-8")
    # Non-matching extension should be skipped
    (tmp_path / "c.bin").write_bytes(b"\x00\x01\x02")

    content, source_type = _read_source(str(tmp_path))
    assert source_type == "directory"
    assert "File A" in content
    assert "File B" in content
    assert "\x00" not in content  # binary file excluded


def test_read_source_directory_skips_hidden(tmp_path: Path):
    (tmp_path / "visible.md").write_text("visible\n", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("secret\n", encoding="utf-8")

    content, source_type = _read_source(str(tmp_path))
    assert "visible" in content
    assert "secret" not in content


def test_read_source_not_found():
    with pytest.raises(FileNotFoundError):
        _read_source("/nonexistent/path/to/file.md")


def test_read_source_empty_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="No readable files"):
        _read_source(str(tmp_path))


# ── Ingest pipeline ────────────────────────────────────────────────


def _stub_executor(parent: str, direction: str, child: str, prompt_path=None) -> str:
    """Test executor that returns a simple summary."""
    return f"# Summary\n\nThis is a summary of the source material.\n"


def _stub_routing_judge_with_link(content, candidates, prompt_path=None):
    """Test routing judge that returns a LINK action if candidates exist."""
    actions = []
    if candidates:
        actions.append(RoutingAction(
            action="LINK",
            target=candidates[0]["source"],
            direction="related content",
            reason="test",
        ))
    return RoutingResult(actions=actions)


def test_ingest_creates_summary(tmp_path: Path):
    """Ingest a file with --no-route, verify summary page is created."""
    # Create source file
    source = tmp_path / "input" / "paper.md"
    source.parent.mkdir()
    source.write_text("# Research Paper\n\nFindings about memory.\n", encoding="utf-8")

    # Set up tree root
    tree_root = tmp_path / "tree"
    tree_root.mkdir()

    result = ingest(
        str(source),
        root=tree_root,
        no_route=True,
        executor_fn=_stub_executor,
    )

    assert result.source_type == "file"
    assert result.summary_path is not None
    assert result.summary_path.exists()
    content = result.summary_path.read_text(encoding="utf-8")
    assert "Summary" in content


def test_ingest_dry_run_does_not_execute(tmp_path: Path):
    """Dry run routes but doesn't execute actions."""
    source = tmp_path / "paper.md"
    source.write_text("# Paper\n\nContent.\n", encoding="utf-8")

    tree_root = tmp_path / "tree"
    tree_root.mkdir()

    # Create a file that search might find
    concept = tree_root / "concepts" / "memory.md"
    concept.parent.mkdir(parents=True)
    concept.write_text("# Memory Systems\n\nExisting content.\n", encoding="utf-8")

    # Use a stub that returns a REVISE action
    def revise_judge(content, candidates, prompt_path=None):
        if candidates:
            return RoutingResult(actions=[RoutingAction(
                action="REVISE",
                target=candidates[0]["source"],
                direction="update with new info",
                reason="test",
            )])
        return RoutingResult()

    result = ingest(
        str(source),
        root=tree_root,
        dry_run=True,
        executor_fn=_stub_executor,
        routing_judge_fn=revise_judge,
    )

    # Actions should be in the result but not executed
    # The concept file should be unchanged
    assert concept.read_text(encoding="utf-8") == "# Memory Systems\n\nExisting content.\n"


def test_ingest_result_fields():
    """IngestResult has expected defaults."""
    result = IngestResult(source="test.md", source_type="file")
    assert result.summary_path is None
    assert result.routing.actions == []
    assert result.cascade_queued == 0
    assert result.elapsed_ms == 0

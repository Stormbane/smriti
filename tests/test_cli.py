"""The `smriti` command: reads never take the index's write lock; failures are one clean line."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smriti import cli
from smriti.core.tree import tree_root


@pytest.fixture
def indexed_root() -> Path:
    from smriti.store.indexer import index_tree

    root = tree_root()
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "lantern.md").write_text(
        "# Lantern\n\nSam chose SQLite for the Lantern storage layer.\n", encoding="utf-8")
    index_tree(root=root)
    return root


def test_read_works_while_a_writer_holds_the_index(
        indexed_root: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A read that opened the index for writing would wait on the writer and fail."""
    monkeypatch.setenv("SMRITI_DB_BUSY_TIMEOUT_MS", "1000")
    writer = sqlite3.connect(str(indexed_root / ".smriti" / "index.db"))
    writer.execute("BEGIN IMMEDIATE")  # hold the write lock, as a long index run does
    try:
        assert cli.main(["read", "Lantern storage", "--no-rerank"]) == 0
    finally:
        writer.rollback()
        writer.close()
    assert "notes/lantern.md" in capsys.readouterr().out


def test_a_failing_command_prints_one_line_and_exits_1(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(args: object) -> int:
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(cli, "_cmd_status", boom)
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "smriti status: RuntimeError: the index is on fire" in err
    assert "Traceback" not in err and "-v" in err


def test_verbose_shows_the_traceback(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    def boom(args: object) -> int:
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(cli, "_cmd_status", boom)
    assert cli.main(["-v", "status"]) == 1
    assert "Traceback" in capsys.readouterr().err

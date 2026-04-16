"""Tests for the wake system (wake.py session-start loader)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WAKE_PY = REPO_ROOT / "narada" / ".smriti" / "wake.py"


@pytest.fixture()
def wake_tree(tmp_path: Path) -> Path:
    """Minimal memory root with wake-context and identity files."""
    # .smriti/ with wake-context.md
    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()
    (smriti_dir / "wake-context.md").write_text(
        "# Test Entity\nI am the test entity. Test beliefs. Test practices.\n"
    )

    # Identity files at new locations
    (tmp_path / "identity.md").write_text("# Identity\nI am the test entity.\n")
    mind_desires = tmp_path / "mind" / "desires"
    mind_desires.mkdir(parents=True)
    (tmp_path / "mind" / "mind.md").write_text("# Mind\nTest mind synthesis.\n")
    (mind_desires / "beliefs.md").write_text("# Beliefs\nTest beliefs.\n")
    (mind_desires / "values.md").write_text("# Values\nTest values.\n")

    # Project mirror
    mirrors_ai = tmp_path / "mirrors" / "testproject" / "ai"
    mirrors_ai.mkdir(parents=True)
    (mirrors_ai / "todo.md").write_text("# TODO\n- [ ] Test task\n")
    mirrors_mem = tmp_path / "mirrors" / "testproject" / "auto-memory"
    mirrors_mem.mkdir(parents=True)
    (mirrors_mem / "MEMORY.md").write_text("# Memory\nTest memory index.\n")

    # Patch wake.py to use this tree instead of ~/.narada
    wake_src = WAKE_PY.read_text(encoding="utf-8")
    wake_src = wake_src.replace(
        'NARADA = Path.home() / ".narada"',
        f'NARADA = Path(r"{tmp_path}")',
    )
    (smriti_dir / "wake.py").write_text(wake_src, encoding="utf-8")

    return tmp_path


def _run_wake(wake_tree: Path, env_vars: dict | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SMRITI_WAKE", None)
    if env_vars:
        env.update(env_vars)
    return subprocess.run(
        [sys.executable, str(wake_tree / ".smriti" / "wake.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or str(Path.cwd()),
        timeout=10,
    )


class TestWakeEnvGating:
    def test_silent_when_unset(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_silent_when_zero(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "0"})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_silent_when_skip(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "skip"})
        assert result.returncode == 0
        assert result.stdout == ""

    def test_fires_when_one(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert result.returncode == 0
        assert "test entity" in result.stdout
        assert "READING LIST" in result.stdout

    def test_fires_when_full(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "full"})
        assert result.returncode == 0
        assert "IDENTITY" in result.stdout

    def test_fires_when_true(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "true"})
        assert result.returncode == 0
        assert "IDENTITY" in result.stdout


class TestWakeSections:
    def test_context_loaded(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert "test entity" in result.stdout
        assert "IDENTITY" in result.stdout

    def test_project_files_loaded(self, wake_tree: Path) -> None:
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree / "mirrors" / "testproject"),
        )
        assert "Test task" in result.stdout
        assert "Test memory" in result.stdout

    def test_unknown_project_skips_gracefully(self, wake_tree: Path) -> None:
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree),  # cwd basename won't match any mirror
        )
        assert result.returncode == 0
        assert "IDENTITY" in result.stdout

    def test_reading_list_ordered(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert "READING LIST" in result.stdout
        assert "open-threads" in result.stdout
        assert "beliefs.md" in result.stdout
        assert "values.md" in result.stdout


class TestRecentJournal:
    def test_journal_tail_emitted(self, wake_tree: Path) -> None:
        journal = wake_tree / "journal" / "2026" / "04" / "week3"
        journal.mkdir(parents=True)
        (journal / "04-15.md").write_text("---\ndate: 2026-04-15\n---\n\n# Yesterday\n\nYesterday's thoughts.\n")
        (journal / "04-16.md").write_text("---\ndate: 2026-04-16\n---\n\n# Today\n\nToday's thoughts.\n")

        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert "RECENT JOURNAL" in result.stdout
        assert "Yesterday's thoughts" in result.stdout
        assert "Today's thoughts" in result.stdout

    def test_journal_empty_gracefully(self, wake_tree: Path) -> None:
        """If journal dir doesn't exist, no error."""
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert result.returncode == 0
        assert "RECENT JOURNAL" not in result.stdout

    def test_truncation_notice_when_journal_cut(self, wake_tree: Path) -> None:
        """Large journal entries should produce truncation notices."""
        journal = wake_tree / "journal" / "2026" / "04" / "week3"
        journal.mkdir(parents=True)
        # Write a very large journal entry that will exceed the journal budget
        big_content = "\n".join(["This is a long journal entry line." for _ in range(300)])
        (journal / "04-16.md").write_text(f"---\ndate: 2026-04-16\n---\n\n# Big\n\n{big_content}\n")

        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert "Truncated" in result.stdout


class TestWakeEdgeCases:
    def test_missing_context_file(self, wake_tree: Path) -> None:
        (wake_tree / ".smriti" / "wake-context.md").unlink()
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert result.returncode == 0
        # Reading list should still appear
        assert "READING LIST" in result.stdout

    def test_output_under_budget(self, wake_tree: Path) -> None:
        """Total output must stay under 10K characters."""
        # Add journal entries to stress the budget
        journal = wake_tree / "journal" / "2026" / "04" / "week3"
        journal.mkdir(parents=True)
        for day in range(14, 17):
            content = f"Entry for day {day}. " * 100
            (journal / f"04-{day}.md").write_text(
                f"---\ndate: 2026-04-{day}\n---\n\n# Day {day}\n\n{content}\n"
            )

        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert result.returncode == 0
        assert len(result.stdout) <= 10000, f"Wake output {len(result.stdout)} chars exceeds 10K limit"

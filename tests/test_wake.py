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
    (mirrors_ai / "STATUS.md").write_text("# STATUS\nTest current state.\n")
    (mirrors_ai / "INDEX.md").write_text("# INDEX\nTest project routing.\n")
    mirrors_mem = tmp_path / "mirrors" / "testproject" / "auto-memory"
    mirrors_mem.mkdir(parents=True)
    (mirrors_mem / "MEMORY.md").write_text("# Memory\nTest memory index.\n")

    # Copy wake.py as-is — we'll set SMRITI_ROOT in the env to point at this tree
    import shutil
    shutil.copy2(WAKE_PY, smriti_dir / "wake.py")

    return tmp_path


def _run_wake(
    wake_tree: Path, env_vars: dict | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("SMRITI_WAKE", None)
    env["SMRITI_ROOT"] = str(wake_tree)  # Point wake.py at the test tree
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
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

    def test_targeted_context_requires_matching_audience(self, wake_tree: Path) -> None:
        targeted = wake_tree / ".smriti" / "context"
        targeted.mkdir()
        (targeted / "hermes.md").write_text("Hermes-only routing.\n")

        coding = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        hermes = _run_wake(
            wake_tree, {"SMRITI_WAKE": "1", "SMRITI_WAKE_AUDIENCE": "hermes"}
        )

        assert "Hermes-only routing." not in coding.stdout
        assert "Hermes-only routing." in hermes.stdout

    def test_project_files_loaded(self, wake_tree: Path) -> None:
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree / "mirrors" / "testproject"),
        )
        assert "Test current state" in result.stdout
        assert "Test project routing" in result.stdout
        assert "Test memory" not in result.stdout

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
        (journal / "04-15.md").write_text(
            "---\ndate: 2026-04-15\n---\n\n# Yesterday\n\nYesterday's thoughts.\n"
        )
        (journal / "04-16.md").write_text(
            "---\ndate: 2026-04-16\n---\n\n# Today\n\nToday's thoughts.\n"
        )

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
        assert len(result.stdout) <= 10000, (
            f"Wake output {len(result.stdout)} chars exceeds 10K limit"
        )


class TestIdentityFallback:
    """H14.4 (Opus, outside review #7): nothing creates ``.smriti/wake-context.md``, so a
    new root woke with no identity section. Without it, the section is the trunk's
    identity files; with it, the wake is what it was."""

    def test_a_fresh_root_from_the_template_shows_its_identity(
            self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from smriti.install.core import install_memory_template
        from smriti.wake.briefing import briefing

        root = tmp_path / "root"
        root.mkdir()
        install_memory_template(root)
        capsys.readouterr()
        assert not (root / ".smriti" / "wake-context.md").exists()
        identity = (root / "identity.md").read_text(encoding="utf-8").strip()
        mind = (root / "mind" / "mind.md").read_text(encoding="utf-8").strip()
        assert "This file should be authored" in identity

        text = briefing(root, cwd=tmp_path)
        section = text.split("--- IDENTITY ---\n", 1)[1]
        assert section.startswith(
            f"### identity.md\n{identity}\n\n### mind/mind.md\n{mind}\n")

    def test_a_long_identity_is_capped_and_named_in_the_notices(
            self, tmp_path: Path) -> None:
        from smriti.wake.briefing import CONTEXT_CAP, briefing

        (tmp_path / "identity.md").write_text(
            "# Identity\n" + "I am Ari, and I keep notes for Sam.\n" * 300, encoding="utf-8")
        text = briefing(tmp_path, cwd=tmp_path)
        start = text.index("--- IDENTITY ---\n")
        section = text[start:text.index("\n\n", start)]
        assert section.startswith("--- IDENTITY ---\n### identity.md\n# Identity\n")
        assert len(section) <= len("--- IDENTITY ---\n") + CONTEXT_CAP
        assert f"Read full: {tmp_path / 'identity.md'}" in text  # mind.md is absent

    def test_no_identity_files_and_no_context_means_no_section(self, tmp_path: Path) -> None:
        from smriti.wake.briefing import briefing

        (tmp_path / "identity.md").write_text("  \n", encoding="utf-8")  # empty: left out
        assert "--- IDENTITY ---" not in briefing(tmp_path, cwd=tmp_path)

    def test_a_wake_context_is_used_as_it_was_whatever_the_identity_files_hold(
            self, wake_tree: Path) -> None:
        from smriti.wake.briefing import briefing

        context = (wake_tree / ".smriti" / "wake-context.md").read_text(encoding="utf-8")
        with_files = briefing(wake_tree, cwd=wake_tree)
        assert f"--- IDENTITY ---\n{context.rstrip()}\n" in with_files
        assert "### identity.md" not in with_files
        (wake_tree / "identity.md").unlink()
        (wake_tree / "mind" / "mind.md").unlink()
        assert briefing(wake_tree, cwd=wake_tree) == with_files

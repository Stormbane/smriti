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
    """Minimal memory root with wake.md and identity files."""
    (tmp_path / "identity.md").write_text("# Identity\nI am the test entity.\n")
    (tmp_path / "mind.md").write_text("# Mind\nTest beliefs.\n")
    (tmp_path / "practices.md").write_text("# Practices\nTest practices.\n")

    smriti_dir = tmp_path / ".smriti"
    smriti_dir.mkdir()

    wake_md = tmp_path / "wake.md"
    wake_md.write_text(
        "# wake.md\n\n"
        "## always\n\n"
        "identity.md\n"
        "mind.md\n"
        "practices.md\n\n"
        "## current-project\n\n"
        "mirrors/{project}/working/working.md\n"
    )

    mirrors = tmp_path / "mirrors" / "testproject" / "working"
    mirrors.mkdir(parents=True)
    (mirrors / "working.md").write_text("# Working\nLast session state.\n")

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
        assert "Identity" in result.stdout
        assert "Mind" in result.stdout
        assert "Practices" in result.stdout

    def test_fires_when_full(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "full"})
        assert result.returncode == 0
        assert "Identity" in result.stdout

    def test_fires_when_true(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "true"})
        assert result.returncode == 0
        assert "Identity" in result.stdout


class TestWakeSections:
    def test_always_section_loaded(self, wake_tree: Path) -> None:
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert "test entity" in result.stdout
        assert "Test beliefs" in result.stdout
        assert "Test practices" in result.stdout

    def test_current_project_resolved(self, wake_tree: Path) -> None:
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree / "mirrors" / "testproject"),
        )
        assert "Last session state" in result.stdout

    def test_unknown_project_skips_gracefully(self, wake_tree: Path) -> None:
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree),  # cwd basename won't match any mirror
        )
        assert result.returncode == 0
        assert "Identity" in result.stdout

    def test_mirrors_list_shown(self, wake_tree: Path) -> None:
        other = wake_tree / "mirrors" / "other-project"
        other.mkdir(parents=True)
        result = _run_wake(
            wake_tree,
            {"SMRITI_WAKE": "1"},
            cwd=str(wake_tree / "mirrors" / "testproject"),
        )
        assert "other-project" in result.stdout


class TestWakeEdgeCases:
    def test_missing_wake_md(self, tmp_path: Path) -> None:
        smriti_dir = tmp_path / ".smriti"
        smriti_dir.mkdir()
        wake_src = WAKE_PY.read_text(encoding="utf-8")
        wake_src = wake_src.replace(
            'NARADA = Path.home() / ".narada"',
            f'NARADA = Path(r"{tmp_path}")',
        )
        (smriti_dir / "wake.py").write_text(wake_src, encoding="utf-8")

        env = os.environ.copy()
        env["SMRITI_WAKE"] = "1"
        result = subprocess.run(
            [sys.executable, str(smriti_dir / "wake.py")],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_missing_identity_file(self, wake_tree: Path) -> None:
        (wake_tree / "identity.md").unlink()
        result = _run_wake(wake_tree, {"SMRITI_WAKE": "1"})
        assert result.returncode == 0
        assert "Mind" in result.stdout

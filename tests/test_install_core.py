"""Tests for src/smriti/install/core.py.

The Phase 9 install_wake_files upgrade logic decides whether to
overwrite an existing wake.py / backup.py / narada-p.sh on re-install.
Getting the rule wrong either nukes user customizations (false
positive) or strands users on stale shims (false negative). Pin all
four branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smriti.install import core


class TestInstallWakeFilesUpgradePath:
    """Four branches: fresh / identical / stale-managed / hand-customized."""

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_fresh_install_copies_template(self, tmp_path):
        memory_root = tmp_path / "narada"
        core.install_wake_files(memory_root)
        wake = memory_root / ".smriti" / "wake.py"
        assert wake.exists()
        # Should match the repo template byte-for-byte.
        template = core.TEMPLATES / ".smriti" / "wake.py"
        assert wake.read_bytes() == template.read_bytes()

    def test_identical_rerun_is_noop(self, tmp_path, capsys):
        memory_root = tmp_path / "narada"
        core.install_wake_files(memory_root)
        capsys.readouterr()  # flush

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out
        assert "up to date" in out
        # No backup created on no-op.
        assert not (memory_root / ".smriti" / "wake.py.pre-upgrade.bak").exists()

    def test_stale_managed_shim_upgraded_with_backup(self, tmp_path, capsys):
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        # Old smriti-managed wake.py (matches the marker rule: 'smriti'
        # in first 400 bytes via the docstring).
        old = (
            '"""wake.py — old smriti SessionStart loader.\n\n'
            "Legacy 322-line monolithic implementation.\n"
            '"""\n'
            "# (more old code)\n"
        )
        (smriti_dir / "wake.py").write_text(old, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: log line announces upgrade with backup.
        assert "upgraded wake.py" in out
        backup = smriti_dir / "wake.py.pre-upgrade.bak"
        assert backup.exists()
        assert self._read(backup) == old

        # New file matches template.
        template = core.TEMPLATES / ".smriti" / "wake.py"
        assert (smriti_dir / "wake.py").read_bytes() == template.read_bytes()

    def test_hand_customized_left_alone(self, tmp_path, capsys):
        """No 'smriti' marker -> don't touch, don't back up."""
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        custom = "# my own wake script\n# I do something custom\nprint('hi')\n"
        (smriti_dir / "wake.py").write_text(custom, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: log warning, no backup.
        assert "not smriti-managed" in out
        assert not (smriti_dir / "wake.py.pre-upgrade.bak").exists()
        # File untouched.
        assert self._read(smriti_dir / "wake.py") == custom

    def test_marker_check_is_case_insensitive(self, tmp_path, capsys):
        """Phase 9 detection lowercases first 400 bytes; uppercase
        SMRITI in a docstring should still trigger upgrade."""
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        old_uppercase = '"""SMRITI WAKE LEGACY """\n# old impl\n'
        (smriti_dir / "wake.py").write_text(old_uppercase, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out
        assert "upgraded wake.py" in out

    def test_template_missing_logs_and_continues(self, tmp_path, monkeypatch, capsys):
        """If a template file disappears we should log and continue,
        not crash mid-install."""
        memory_root = tmp_path / "narada"
        # Point TEMPLATES at a directory missing the wake template.
        broken_templates = tmp_path / "broken"
        broken_templates.mkdir()
        (broken_templates / ".smriti").mkdir()
        # Only backup.py exists; wake.py + narada-p.sh missing.
        (broken_templates / ".smriti" / "backup.py").write_text(
            "# smriti backup\n", encoding="utf-8"
        )
        monkeypatch.setattr(core, "TEMPLATES", broken_templates)

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: prints "template missing" for the absent file,
        # continues to install backup.py.
        assert "template missing" in out
        assert (memory_root / ".smriti" / "backup.py").exists()

    def test_creates_smriti_dir_if_missing(self, tmp_path):
        memory_root = tmp_path / "narada"
        assert not memory_root.exists()
        core.install_wake_files(memory_root)
        assert (memory_root / ".smriti").is_dir()


class TestPlatformHelpers:
    def test_is_windows_consistent_with_platform(self):
        import sys
        assert core.is_windows() == (sys.platform == "win32")

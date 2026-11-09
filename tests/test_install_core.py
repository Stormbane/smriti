"""Tests for src/smriti/install/core.py.

The install_wake_files upgrade logic decides whether to overwrite a
deployed script on re-install, and whether to remove one an earlier
install deployed that nothing runs any more. Getting the rule wrong
either nukes user customizations (false positive) or strands users on
stale shims (false negative). Pin all the branches.
"""

from __future__ import annotations

from pathlib import Path


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
        assert core.SCRIPTS_DIR is not None
        template = core.SCRIPTS_DIR / "wake.py"
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
        assert core.SCRIPTS_DIR is not None
        template = core.SCRIPTS_DIR / "wake.py"
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
        # Point SCRIPTS_DIR at a directory missing one of the templates.
        broken_templates = tmp_path / "broken" / ".smriti"
        broken_templates.mkdir(parents=True)
        (broken_templates / "present.sh").write_text("# smriti\n", encoding="utf-8")
        monkeypatch.setattr(core, "SCRIPTS_DIR", broken_templates)
        monkeypatch.setattr(core, "DEPLOYED", ("absent.sh", "present.sh"))

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: prints "template missing" for the absent file,
        # continues to install the present one.
        assert "template missing" in out and "absent.sh" in out
        assert (memory_root / ".smriti" / "present.sh").exists()

    def test_creates_smriti_dir_if_missing(self, tmp_path):
        memory_root = tmp_path / "narada"
        assert not memory_root.exists()
        core.install_wake_files(memory_root)
        assert (memory_root / ".smriti").is_dir()


class TestRetiredScriptsAreRemoved:
    """Scripts an earlier install deployed and nothing runs any more."""

    def test_a_smriti_managed_copy_is_removed_and_said_so(self, tmp_path, capsys):
        smriti_dir = tmp_path / "ari" / ".smriti"
        smriti_dir.mkdir(parents=True)
        for name in core.RETIRED:
            (smriti_dir / name).write_text(f'"""{name}: a smriti script."""\n',
                                           encoding="utf-8")

        core.install_wake_files(tmp_path / "ari")
        out = capsys.readouterr().out

        for name, instead in core.RETIRED.items():
            assert not (smriti_dir / name).exists()
            assert f"removed {smriti_dir / name}: {instead}" in out
        # git history keeps the copy: no .bak in the tree.
        assert not [p for p in smriti_dir.iterdir() if p.name.endswith(".bak")]

    def test_a_hand_written_one_is_left_with_a_warning(self, tmp_path, capsys):
        smriti_dir = tmp_path / "ari" / ".smriti"
        smriti_dir.mkdir(parents=True)
        mine = "# Sam's own backup\nprint('hi')\n"
        for name in core.RETIRED:
            (smriti_dir / name).write_text(mine, encoding="utf-8")

        core.install_wake_files(tmp_path / "ari")
        out = capsys.readouterr().out

        for name in core.RETIRED:
            assert (smriti_dir / name).read_text(encoding="utf-8") == mine
            assert f"{smriti_dir / name} is not smriti-managed" in out
        assert "nothing runs it any more" in out

    def test_none_left_says_nothing(self, tmp_path, capsys):
        core.install_wake_files(tmp_path / "ari")
        assert "removed" not in capsys.readouterr().out

    def test_retired_copies_go_even_without_templates(self, tmp_path, monkeypatch):
        smriti_dir = tmp_path / "ari" / ".smriti"
        smriti_dir.mkdir(parents=True)
        for name in core.RETIRED:
            (smriti_dir / name).write_text("# smriti\n", encoding="utf-8")
        monkeypatch.setattr(core, "SCRIPTS_DIR", None)
        core.install_wake_files(tmp_path / "ari")
        assert not any((smriti_dir / name).exists() for name in core.RETIRED)

    def test_no_retired_script_is_deployed(self):
        assert not set(core.RETIRED) & set(core.DEPLOYED)
        assert core.SCRIPTS_DIR is not None
        assert not any((core.SCRIPTS_DIR / name).exists() for name in core.RETIRED)


class TestPlatformHelpers:
    def test_is_windows_consistent_with_platform(self):
        import sys
        assert core.is_windows() == (sys.platform == "win32")

"""Tests for src/smriti/install/core.py.

The install_wake_files upgrade logic decides whether to overwrite a
deployed script on re-install, and whether to remove one an earlier
install deployed that nothing runs any more. Getting the rule wrong
either nukes user customizations (false positive) or strands users on
stale shims (false negative). Pin all the branches.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from smriti.install import core
from smriti.integrations.common.hook_model import make_wake_hook_command

SCRIPT = "narada-p.sh"  # the one script the installer deploys


class TestInstallWakeFilesUpgradePath:
    """Four branches: fresh / identical / stale-managed / hand-customized."""

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_fresh_install_copies_template(self, tmp_path):
        memory_root = tmp_path / "narada"
        core.install_wake_files(memory_root)
        wake = memory_root / ".smriti" / SCRIPT
        assert wake.exists()
        # Should match the repo template byte-for-byte.
        assert core.SCRIPTS_DIR is not None
        template = core.SCRIPTS_DIR / SCRIPT
        assert wake.read_bytes() == template.read_bytes()

    def test_identical_rerun_is_noop(self, tmp_path, capsys):
        memory_root = tmp_path / "narada"
        core.install_wake_files(memory_root)
        capsys.readouterr()  # flush

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out
        assert "up to date" in out
        # No backup created on no-op.
        assert not (memory_root / ".smriti" / "narada-p.sh.pre-upgrade.bak").exists()

    def test_stale_managed_shim_upgraded_with_backup(self, tmp_path, capsys):
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        # An old smriti-managed script (matches the marker rule: 'smriti'
        # in first 400 bytes, its header comment).
        old = (
            "#!/usr/bin/env bash\n"
            "# smriti-p.sh — an old smriti headless wrapper.\n"
            "# (more old code)\n"
        )
        (smriti_dir / SCRIPT).write_text(old, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: log line announces upgrade with backup.
        assert "upgraded narada-p.sh" in out
        backup = smriti_dir / "narada-p.sh.pre-upgrade.bak"
        assert backup.exists()
        assert self._read(backup) == old

        # New file matches template.
        assert core.SCRIPTS_DIR is not None
        template = core.SCRIPTS_DIR / SCRIPT
        assert (smriti_dir / SCRIPT).read_bytes() == template.read_bytes()

    def test_hand_customized_left_alone(self, tmp_path, capsys):
        """No 'smriti' marker -> don't touch, don't back up."""
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        custom = "#!/bin/sh\n# my own wrapper\n# I do something custom\necho hi\n"
        (smriti_dir / SCRIPT).write_text(custom, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out

        # Behavior: log warning, no backup.
        assert "not smriti-managed" in out
        assert not (smriti_dir / "narada-p.sh.pre-upgrade.bak").exists()
        # File untouched.
        assert self._read(smriti_dir / SCRIPT) == custom

    def test_marker_check_is_case_insensitive(self, tmp_path, capsys):
        """Phase 9 detection lowercases first 400 bytes; uppercase
        SMRITI in a docstring should still trigger upgrade."""
        memory_root = tmp_path / "narada"
        smriti_dir = memory_root / ".smriti"
        smriti_dir.mkdir(parents=True)

        old_uppercase = "# SMRITI-P LEGACY\n# old impl\n"
        (smriti_dir / SCRIPT).write_text(old_uppercase, encoding="utf-8")

        core.install_wake_files(memory_root)
        out = capsys.readouterr().out
        assert "upgraded narada-p.sh" in out

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


# The SessionStart hook of an install made before the wake ran from the package,
# as the reference machine has it, and the two scripts that install deployed.
EARLIER_HOOK = 'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"'
EARLIER_WAKE = '"""wake.py — SessionStart shim for smriti-managed entities."""\n'
EARLIER_BACKUP = ('#!/usr/bin/env python\n"""Backup helper for the memory tree.\n\n'
                  'Quietly commits the SMRITI_ROOT git repo."""\n')
ACTIVITY = 'touch "$HOME/.narada/.smriti/last-activity" 2>/dev/null || true'


def _install(home: Path, root: Path) -> int:
    """scripts/install.py for Claude Code, as a person reruns it, without a terminal."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
    spec = importlib.util.spec_from_file_location("smriti_install_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.main(["--harness", "claude_code", "--memory-root", str(root),
                          "--projects-root", str(home / "no-projects"), "--no-capture"])
    assert isinstance(result, int)
    return result


def test_a_reinstall_moves_an_earlier_install_off_the_trees_code(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The memory tree held the wake and the backup that ran every session start and
    every hour. A reinstall rewrites the hook to the package's wake, in place and
    once, and removes the two scripts, with no .bak (git history keeps them)."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(core, "HOME", home)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    root = home / ".narada"
    (root / ".smriti").mkdir(parents=True)
    (root / ".smriti" / "wake.py").write_text(EARLIER_WAKE, encoding="utf-8")
    (root / ".smriti" / "backup.py").write_text(EARLIER_BACKUP, encoding="utf-8")
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": EARLIER_HOOK}]}],
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": ACTIVITY}]}],
    }}), encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")

    assert _install(home, root) == 0

    out = capsys.readouterr().out
    smriti_dir = root / ".smriti"
    assert not (smriti_dir / "wake.py").exists() and not (smriti_dir / "backup.py").exists()
    assert f"[wake] removed {smriti_dir / 'wake.py'}: " in out
    assert f"[wake] removed {smriti_dir / 'backup.py'}: " in out
    assert not [p.name for p in smriti_dir.iterdir() if ".bak" in p.name]
    assert (smriti_dir / "narada-p.sh").is_file()
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert hooks["SessionStart"] == [{"matcher": "", "hooks": [
        {"type": "command",
         "command": make_wake_hook_command(root, home=home, framing="raw")}]}]
    assert hooks["UserPromptSubmit"] == [{"matcher": "", "hooks": [
        {"type": "command", "command": ACTIVITY}]}]
    assert "SessionStart wake hook upgraded to canonical form" in out
    doc = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "`python -m smriti.wake`" in doc and "wake.py" not in doc

    # A second run finds nothing to do.
    before = settings.read_bytes()
    assert _install(home, root) == 0
    out = capsys.readouterr().out
    assert "removed" not in out
    assert settings.read_bytes() == before


class TestPlatformHelpers:
    def test_is_windows_consistent_with_platform(self):
        import sys
        assert core.is_windows() == (sys.platform == "win32")

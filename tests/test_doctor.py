from __future__ import annotations

import json
import sys
from pathlib import Path

from smriti.doctor import Check, _briefing_checks, claude_code_checks, codex_checks, run_doctor
from smriti.integrations.claude_code import install as claude_install
from smriti.integrations.codex import install as codex_install
from smriti.integrations.common.hook_model import make_wake_hook_command
from tests._home import use_home


def _make_entity_and_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    memory_root = home / ".narada"
    smriti_dir = memory_root / ".smriti"
    smriti_dir.mkdir(parents=True)
    (smriti_dir / "wake-context.md").write_text("# Narada\nI am Narada.\n")
    project = tmp_path / "beautiful-tree"
    ai = project / ".ai"
    ai.mkdir(parents=True)
    (ai / "STATUS.md").write_text("# STATUS\nCurrent truth.\n")
    (ai / "INDEX.md").write_text("# INDEX\nRoutes work.\n")
    mirrors_ai = memory_root / "mirrors" / project.name / "ai"
    mirrors_ai.mkdir(parents=True)
    (mirrors_ai / "STATUS.md").write_text("# STATUS\nCurrent truth.\n")
    (mirrors_ai / "INDEX.md").write_text("# INDEX\nRoutes work.\n")
    return home, memory_root, project


def test_codex_doctor_accepts_a_freshly_installed_bridge(tmp_path, monkeypatch) -> None:
    home, memory_root, project = _make_entity_and_project(tmp_path)
    codex = home / ".codex"
    codex.mkdir()

    use_home(monkeypatch, home)
    assert codex_install.run_codex(memory_root) is True

    checks = codex_checks(memory_root=memory_root, project=project, home=home)

    assert all(check.passed for check in checks), [c for c in checks if not c.passed]


def test_claude_doctor_flags_deployed_hook_drift(tmp_path, monkeypatch) -> None:
    """Adversarial review finding 4: a stale deployed hook script must FAIL,
    not hide behind passing config checks."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    use_home(monkeypatch, home)
    assert claude_install.run_claude_code(memory_root) is True

    (claude / "hooks" / "precompact_capture.py").write_text("# stale\n", encoding="utf-8")

    checks = {c.name: c
              for c in claude_code_checks(memory_root=memory_root, project=project, home=home)}
    assert checks["Claude deployed hooks"].passed is False
    assert "drifted" in checks["Claude deployed hooks"].detail


def test_claude_doctor_accepts_a_freshly_installed_bridge(tmp_path, monkeypatch) -> None:
    home, memory_root, project = _make_entity_and_project(tmp_path)
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")

    use_home(monkeypatch, home)
    assert claude_install.run_claude_code(memory_root) is True

    checks = claude_code_checks(memory_root=memory_root, project=project, home=home)

    assert all(check.passed for check in checks), [c for c in checks if not c.passed]


def _ledger_check(memory_root: Path, project: Path) -> Check:
    return next(c for c in _briefing_checks(memory_root, project) if c.name == "Project ledger")


def test_the_project_ledger_passes_with_status_and_index(tmp_path) -> None:
    _, memory_root, project = _make_entity_and_project(tmp_path)
    assert _ledger_check(memory_root, project).passed


def test_the_project_ledger_passes_with_the_todo_fallback(tmp_path) -> None:
    # The briefing shows todo.md when a project keeps no STATUS.md or
    # INDEX.md (docs/SELF.md); that is a ledger, not a broken install.
    _, memory_root, project = _make_entity_and_project(tmp_path)
    mirrors_ai = memory_root / "mirrors" / project.name / "ai"
    for name in ("STATUS.md", "INDEX.md"):
        (mirrors_ai / name).unlink()
    (mirrors_ai / "todo.md").write_text("# TODO\n- [ ] ship\n")
    assert _ledger_check(memory_root, project).passed


def test_the_project_ledger_fails_without_one(tmp_path) -> None:
    _, memory_root, project = _make_entity_and_project(tmp_path)
    for name in ("STATUS.md", "INDEX.md"):
        (memory_root / "mirrors" / project.name / "ai" / name).unlink()
    assert not _ledger_check(memory_root, project).passed


def test_claude_doctor_flags_missing_hook_target(tmp_path, monkeypatch) -> None:
    """Adversarial review finding 3 hardening: a hook command pointing
    at a deleted script must FAIL loudly — hooks fail silent at runtime."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    claude = home / ".claude"
    claude.mkdir()
    wake_cmd = make_wake_hook_command(memory_root, home=home, framing="raw")
    settings = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": wake_cmd}]}
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python ~/.claude/hooks/deleted_hook.py",
                        }
                    ],
                }
            ],
        }
    }
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    checks = {
        c.name: c
        for c in claude_code_checks(memory_root=memory_root, project=project, home=home)
    }
    assert checks["Claude wake hook"].passed is True
    assert checks["Claude hook targets exist"].passed is False
    assert "deleted_hook.py" in checks["Claude hook targets exist"].detail


def test_run_doctor_rejects_unknown_harness(tmp_path) -> None:
    assert run_doctor(harness="cursor", memory_root=tmp_path, project=tmp_path) == 2


def test_codex_doctor_tolerates_hooks_state_table(tmp_path, monkeypatch) -> None:
    """The live Codex config keeps a [hooks.state] trust registry next to
    the event arrays; doctor must not crash walking it (live crash,
    2026-07-17)."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    use_home(monkeypatch, home)
    codex_install.run_codex(memory_root)

    config = codex / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[hooks.state]\n"
        + '[hooks.state."config:session_start:0:0"]\n'
        + 'trusted_hash = "sha256:abc"\nenabled = true\n',
        encoding="utf-8",
    )

    checks = codex_checks(memory_root=memory_root, project=project, home=home)
    assert all(check.passed for check in checks), [c for c in checks if not c.passed]


def _claude_settings(home: Path, wake_cmd: str) -> None:
    claude = home / ".claude"
    claude.mkdir(exist_ok=True)
    settings = {"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": wake_cmd}]}]}}
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_doctor_finds_a_hook_script_after_a_quoted_interpreter(tmp_path) -> None:
    """H15.1: hooks name their interpreter by a quoted path. The target check
    found scripts only after a bare python, so a deleted script passed it."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    _claude_settings(home, "'/opt/py env/bin/python3' \"$HOME/.claude/hooks/gone.py\"")

    checks = {c.name: c
              for c in claude_code_checks(memory_root=memory_root, project=project, home=home)}

    assert checks["Claude hook targets exist"].passed is False
    assert "gone.py" in checks["Claude hook targets exist"].detail


def test_the_wake_hook_needs_no_file_in_the_memory_tree(tmp_path) -> None:
    """H15.8: the hook runs the package's wake, so the tree holds no script for it."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    _claude_settings(home, make_wake_hook_command(memory_root, home=home, framing="raw"))

    checks = {c.name: c
              for c in claude_code_checks(memory_root=memory_root, project=project, home=home)}

    assert checks["Claude wake hook"].passed is True
    assert checks["Claude hook targets exist"].passed is True
    assert not (memory_root / ".smriti" / "wake.py").exists()


def test_doctor_fails_an_earlier_installs_wake_hook(tmp_path) -> None:
    """Installs before H15.1 wrote a bare python, which the harness looks up on
    its own PATH, and before H15.8 ran the tree's wake.py; doctor names both
    gaps, and a reinstall closes them."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    (memory_root / ".smriti" / "wake.py").write_text('"""smriti wake"""\n', encoding="utf-8")
    _claude_settings(home, 'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" '
                           'python "$HOME/.narada/.smriti/wake.py"')

    checks = {c.name: c
              for c in claude_code_checks(memory_root=memory_root, project=project, home=home)}

    assert checks["Claude wake hook"].passed is False
    assert checks["Claude wake hook"].detail == (
        "runs $HOME/.narada/.smriti/wake.py from the memory tree, not smriti.wake from the "
        f"installed package; runs python from PATH, not {sys.executable}")
    assert checks["Claude hook targets exist"].passed is True

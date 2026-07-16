from __future__ import annotations

import json
from pathlib import Path

from smriti.doctor import claude_code_checks, codex_checks, run_doctor
from smriti.integrations.claude_code import install as claude_install
from smriti.integrations.codex import install as codex_install
from smriti.integrations.common.hook_model import make_wake_hook_command


def _make_entity_and_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    memory_root = home / ".narada"
    smriti_dir = memory_root / ".smriti"
    smriti_dir.mkdir(parents=True)
    (smriti_dir / "wake-context.md").write_text("# Narada\nI am Narada.\n")
    (smriti_dir / "wake.py").write_text("# wake shim\n", encoding="utf-8")
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

    monkeypatch.setattr(codex_install, "HOME", home)
    monkeypatch.setattr(codex_install, "CODEX", codex)
    monkeypatch.setattr(codex_install, "CONFIG_TOML", codex / "config.toml")
    monkeypatch.setattr(codex_install, "AGENTS_MD", codex / "AGENTS.md")
    monkeypatch.setattr(codex_install, "HOOKS_DST", codex / "hooks")
    assert codex_install.run_codex(memory_root) is True

    checks = codex_checks(memory_root=memory_root, project=project, home=home)

    assert all(check.passed for check in checks), [c for c in checks if not c.passed]


def test_codex_doctor_flags_deployed_hook_drift(tmp_path, monkeypatch) -> None:
    """Adversarial review finding 4: a stale deployed shim must FAIL,
    not hide behind passing config checks."""
    home, memory_root, project = _make_entity_and_project(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    monkeypatch.setattr(codex_install, "HOME", home)
    monkeypatch.setattr(codex_install, "CODEX", codex)
    monkeypatch.setattr(codex_install, "CONFIG_TOML", codex / "config.toml")
    monkeypatch.setattr(codex_install, "AGENTS_MD", codex / "AGENTS.md")
    monkeypatch.setattr(codex_install, "HOOKS_DST", codex / "hooks")
    codex_install.run_codex(memory_root)

    (codex / "hooks" / "recall_hook.py").write_text("# stale\n", encoding="utf-8")

    checks = {c.name: c for c in codex_checks(memory_root=memory_root, project=project, home=home)}
    assert checks["Codex deployed hooks"].passed is False
    assert "drifted" in checks["Codex deployed hooks"].detail


def test_claude_doctor_accepts_a_freshly_installed_bridge(tmp_path, monkeypatch) -> None:
    home, memory_root, project = _make_entity_and_project(tmp_path)
    claude = home / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(claude_install, "HOME", home)
    monkeypatch.setattr(claude_install, "CLAUDE", claude)
    monkeypatch.setattr(claude_install, "SETTINGS", claude / "settings.json")
    monkeypatch.setattr(claude_install, "CLAUDE_MD", claude / "CLAUDE.md")
    monkeypatch.setattr(claude_install, "CLAUDE_CONFIG", home / ".claude.json")
    monkeypatch.setattr(claude_install, "HOOKS_DST", claude / "hooks")
    assert claude_install.run_claude_code(memory_root) is True

    checks = claude_code_checks(memory_root=memory_root, project=project, home=home)

    assert all(check.passed for check in checks), [c for c in checks if not c.passed]


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
    monkeypatch.setattr(codex_install, "HOME", home)
    monkeypatch.setattr(codex_install, "CODEX", codex)
    monkeypatch.setattr(codex_install, "CONFIG_TOML", codex / "config.toml")
    monkeypatch.setattr(codex_install, "AGENTS_MD", codex / "AGENTS.md")
    monkeypatch.setattr(codex_install, "HOOKS_DST", codex / "hooks")
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

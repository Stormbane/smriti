from __future__ import annotations

from pathlib import Path

from smriti.doctor import codex_checks
from smriti.integrations.codex import install as codex_install


def test_codex_doctor_accepts_a_ready_project(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    codex = home / ".codex"
    codex.mkdir()
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

    monkeypatch.setattr(codex_install, "HOME", home)
    monkeypatch.setattr(codex_install, "CODEX", codex)
    monkeypatch.setattr(codex_install, "CONFIG_TOML", codex / "config.toml")
    monkeypatch.setattr(codex_install, "AGENTS_MD", codex / "AGENTS.md")
    monkeypatch.setattr(codex_install, "HOOKS_DST", codex / "hooks")
    codex_install.patch_config_toml(memory_root)
    codex_install.write_agents_md(memory_root)

    checks = codex_checks(memory_root=memory_root, project=project, home=home)

    assert all(check.passed for check in checks), checks

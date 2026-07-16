"""Regression tests for the diff-review P2s (2026-07-17).

P2-2: mixed legacy/current hook configurations must never end up with
two active smriti wake hooks — the briefing would load twice per
session. Exactly one survives: the first equivalent, else one upgraded
deficient. Applies to both harnesses.
"""

from __future__ import annotations

import json
import tomllib

import pytest

from smriti.integrations.claude_code import install as cc_install
from smriti.integrations.codex import install as codex_install
from smriti.integrations.common.hook_model import (
    EQUIVALENT,
    classify_wake_hook,
    make_wake_hook_command,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path
    claude = home / ".claude"
    claude.mkdir()
    monkeypatch.setattr(cc_install, "HOME", home)
    monkeypatch.setattr(cc_install, "CLAUDE", claude)
    monkeypatch.setattr(cc_install, "SETTINGS", claude / "settings.json")
    monkeypatch.setattr(cc_install, "CLAUDE_MD", claude / "CLAUDE.md")
    monkeypatch.setattr(cc_install, "CLAUDE_CONFIG", home / ".claude.json")
    monkeypatch.setattr(cc_install, "HOOKS_DST", claude / "hooks")
    return home


def _wake_hooks_in(settings: dict, memory_root, home) -> list[str]:
    return [
        h["command"]
        for group in settings["hooks"]["SessionStart"]
        for h in group.get("hooks", [])
        if classify_wake_hook(
            h.get("command", ""), memory_root=memory_root, home=home, framing="raw"
        )[0] != "unrelated"
    ]


class TestClaudeWakeHookDedup:
    def test_equivalent_plus_legacy_deficient_keeps_exactly_one(self, fake_home):
        """The P2 scenario: an equivalent hook AND a stale legacy hook.
        The legacy one must be removed, not upgraded into a duplicate."""
        memory_root = fake_home / ".narada"
        equivalent = make_wake_hook_command(memory_root, home=fake_home, framing="raw")
        legacy_deficient = 'python "$HOME/.narada/.smriti/wake.py"'  # no env
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": equivalent},
                        {"type": "command", "command": legacy_deficient},
                    ]},
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "echo unrelated"},
                    ]},
                ],
            },
        }), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        wake_cmds = _wake_hooks_in(data, memory_root, fake_home)
        assert len(wake_cmds) == 1
        # The equivalent hook survived byte-for-byte; the legacy is gone.
        assert wake_cmds[0] == equivalent
        # Unrelated hook untouched.
        all_cmds = [
            h["command"]
            for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]
        ]
        assert "echo unrelated" in all_cmds

    def test_two_deficient_hooks_collapse_to_one_canonical(self, fake_home):
        memory_root = fake_home / ".narada"
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({
            "hooks": {
                "SessionStart": [
                    {"matcher": "", "hooks": [
                        {"type": "command",
                         "command": 'python "$HOME/.narada/.smriti/wake.py"'},
                        {"type": "command",
                         "command": 'SMRITI_WAKE=1 python "$HOME/.narada/.smriti/wake.py"'},
                    ]},
                ],
            },
        }), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        wake_cmds = _wake_hooks_in(data, memory_root, fake_home)
        assert len(wake_cmds) == 1
        verdict, detail = classify_wake_hook(
            wake_cmds[0], memory_root=memory_root, home=fake_home, framing="raw"
        )
        assert verdict == EQUIVALENT, detail

    def test_rerun_after_dedup_is_idempotent(self, fake_home):
        memory_root = fake_home / ".narada"
        settings_path = fake_home / ".claude" / "settings.json"
        settings_path.write_text("{}", encoding="utf-8")
        cc_install.patch_settings_json(memory_root)
        first = settings_path.read_text(encoding="utf-8")
        cc_install.patch_settings_json(memory_root)
        assert settings_path.read_text(encoding="utf-8") == first


class TestCodexWakeHookDedup:
    def test_two_equivalent_hooks_keep_only_first(self, tmp_path, monkeypatch):
        home = tmp_path
        codex_dir = home / ".codex"
        codex_dir.mkdir()
        monkeypatch.setattr(codex_install, "HOME", home)
        monkeypatch.setattr(codex_install, "CODEX", codex_dir)
        monkeypatch.setattr(codex_install, "CONFIG_TOML", codex_dir / "config.toml")
        monkeypatch.setattr(codex_install, "AGENTS_MD", codex_dir / "AGENTS.md")
        monkeypatch.setattr(codex_install, "HOOKS_DST", codex_dir / "hooks")
        memory_root = home / ".narada"
        eq = make_wake_hook_command(memory_root, home=home, framing="codex-json")
        config = codex_dir / "config.toml"
        config.write_text(
            "[features]\nhooks = true\n\n"
            "[[hooks.SessionStart]]\n"
            'matcher = "startup|resume|clear|compact"\n'
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            f"command = '{eq}'\n"
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            f"command = '{eq}'\n",
            encoding="utf-8",
        )

        codex_install.patch_config_toml(memory_root)

        data = tomllib.loads(config.read_text(encoding="utf-8"))
        wake_cmds = [
            h["command"]
            for g in data["hooks"]["SessionStart"]
            for h in g["hooks"]
        ]
        assert wake_cmds == [eq]

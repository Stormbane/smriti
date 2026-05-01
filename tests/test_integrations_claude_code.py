"""Tests for src/smriti/integrations/claude_code/install.py.

Idempotent JSON patching of ~/.claude/settings.json + ~/.claude.json
is critical — wrong logic here either misses our hooks (functional
regression) or duplicates entries (bloat that piles up over re-runs).

Tests run against tempdirs via monkeypatch on the module-level path
constants (HOME, CLAUDE, SETTINGS, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smriti.integrations.claude_code import install as cc_install


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect every ~/.claude/* path the install module uses."""
    home = tmp_path
    claude = home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    claude_md = claude / "CLAUDE.md"
    claude_config = home / ".claude.json"
    hooks_dst = claude / "hooks"

    monkeypatch.setattr(cc_install, "HOME", home)
    monkeypatch.setattr(cc_install, "CLAUDE", claude)
    monkeypatch.setattr(cc_install, "SETTINGS", settings)
    monkeypatch.setattr(cc_install, "CLAUDE_MD", claude_md)
    monkeypatch.setattr(cc_install, "CLAUDE_CONFIG", claude_config)
    monkeypatch.setattr(cc_install, "HOOKS_DST", hooks_dst)

    return home


# --- MCP server registration --------------------------------------------

class TestRegisterMCPServer:
    def test_skipped_when_claude_config_missing(self, fake_home, capsys):
        # No ~/.claude.json exists.
        cc_install.register_mcp_server()
        out = capsys.readouterr().out
        assert "not found" in out

    def test_adds_server_to_existing_config(self, fake_home):
        cfg = fake_home / ".claude.json"
        cfg.write_text(json.dumps({"some_other_key": "preserved"}), encoding="utf-8")

        cc_install.register_mcp_server()

        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["some_other_key"] == "preserved"
        assert data["mcpServers"]["smriti"] == {
            "command": "python",
            "args": ["-m", "smriti.mcp_server"],
        }
        # Backup created.
        assert (cfg.with_suffix(".json.bak")).exists()

    def test_idempotent_when_already_registered(self, fake_home, capsys):
        cfg = fake_home / ".claude.json"
        cfg.write_text(json.dumps({
            "mcpServers": {"smriti": {
                "command": "python",
                "args": ["-m", "smriti.mcp_server"],
            }},
        }), encoding="utf-8")

        cc_install.register_mcp_server()
        out = capsys.readouterr().out
        assert "already registered" in out
        # No backup written on no-op.
        assert not (cfg.with_suffix(".json.bak")).exists()

    def test_preserves_other_mcp_servers(self, fake_home):
        cfg = fake_home / ".claude.json"
        cfg.write_text(json.dumps({
            "mcpServers": {"other_server": {"command": "node"}},
        }), encoding="utf-8")

        cc_install.register_mcp_server()

        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert "other_server" in data["mcpServers"]
        assert "smriti" in data["mcpServers"]

    def test_handles_corrupt_json_safely(self, fake_home, capsys):
        cfg = fake_home / ".claude.json"
        cfg.write_text("{not valid json", encoding="utf-8")

        cc_install.register_mcp_server()
        out = capsys.readouterr().out
        assert "parse error" in out
        # File untouched.
        assert cfg.read_text(encoding="utf-8") == "{not valid json"


# --- settings.json hook patching ----------------------------------------

class TestPatchSettingsJSON:
    def test_skipped_when_settings_missing(self, fake_home, capsys):
        cc_install.patch_settings_json(fake_home / ".narada")
        out = capsys.readouterr().out
        assert "not found" in out

    def test_adds_all_three_hooks_to_empty_settings(self, fake_home):
        memory_root = fake_home / ".narada"
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(json.dumps({}), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)
        data = json.loads(settings.read_text(encoding="utf-8"))

        # All three Claude Code hooks wired.
        assert "SessionStart" in data["hooks"]
        assert "UserPromptSubmit" in data["hooks"]
        assert "PostToolUse" in data["hooks"]

        # SessionStart -> wake.py command shape.
        ss_cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "SMRITI_WAKE=1" in ss_cmd
        assert "wake.py" in ss_cmd

        # PostToolUse matcher -> Read|Edit|Write.
        post = data["hooks"]["PostToolUse"]
        assert any(group["matcher"] == "Read|Edit|Write" for group in post)

    def test_idempotent_when_all_hooks_already_wired(self, fake_home, capsys):
        memory_root = fake_home / ".narada"
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(json.dumps({}), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)
        capsys.readouterr()

        # Re-run should be a no-op.
        cc_install.patch_settings_json(memory_root)
        out = capsys.readouterr().out
        assert "already wired" in out

    def test_preserves_unrelated_settings(self, fake_home):
        memory_root = fake_home / ".narada"
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(json.dumps({
            "model": "claude-sonnet-4-6",
            "theme": "dark",
            "hooks": {
                "SessionEnd": [{"matcher": "", "hooks": [{"type": "command",
                                                          "command": "echo bye"}]}]
            },
        }), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)
        data = json.loads(settings.read_text(encoding="utf-8"))

        assert data["model"] == "claude-sonnet-4-6"
        assert data["theme"] == "dark"
        # Pre-existing SessionEnd hook preserved.
        assert "SessionEnd" in data["hooks"]
        # New smriti hooks added.
        assert "SessionStart" in data["hooks"]

    def test_handles_corrupt_json_safely(self, fake_home, capsys):
        memory_root = fake_home / ".narada"
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text("{garbage", encoding="utf-8")

        cc_install.patch_settings_json(memory_root)
        out = capsys.readouterr().out
        assert "parse error" in out

    def test_user_prompt_submit_appends_to_existing_group(self, fake_home):
        """Pre-existing UserPromptSubmit group should get our activity-touch
        appended, not get clobbered."""
        memory_root = fake_home / ".narada"
        settings = fake_home / ".claude" / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "echo other"}],
                }],
            },
        }), encoding="utf-8")

        cc_install.patch_settings_json(memory_root)
        data = json.loads(settings.read_text(encoding="utf-8"))
        ups = data["hooks"]["UserPromptSubmit"]
        # Existing hook preserved AND our activity-touch added.
        cmds = [h["command"] for grp in ups for h in grp["hooks"]]
        assert any("echo other" == c for c in cmds)
        assert any("last-activity" in c for c in cmds)


# --- CLAUDE.md composition ----------------------------------------------

class TestWriteClaudeMD:
    def test_writes_composed_doc(self, fake_home):
        cc_install.write_claude_md(fake_home / ".narada")
        out = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        # Header from compose_agent_doc.
        assert out.startswith("# CLAUDE.md (user-global)")
        # AGENT.md body present.
        assert "## Memory system" in out
        # Claude-Code-specific addendum present.
        assert "Session wake (Claude Code)" in out
        # memory_rel substituted with the right path.
        assert "~/.narada" in out

    def test_idempotent_when_unchanged(self, fake_home, capsys):
        memory_root = fake_home / ".narada"
        cc_install.write_claude_md(memory_root)
        capsys.readouterr()

        cc_install.write_claude_md(memory_root)
        out = capsys.readouterr().out
        assert "up to date" in out


# --- run_claude_code orchestrator ---------------------------------------

class TestRunClaudeCode:
    def test_skip_flags_honored(self, fake_home, capsys):
        memory_root = fake_home / ".narada"
        # Plant minimal settings + claude config so the steps could run.
        (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        (fake_home / ".claude.json").write_text(json.dumps({}), encoding="utf-8")

        cc_install.run_claude_code(memory_root, skip_settings=True, skip_mcp=True)

        # CLAUDE.md still written even with both skips.
        assert (fake_home / ".claude" / "CLAUDE.md").exists()
        # MCP not registered.
        cfg = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))
        assert "mcpServers" not in cfg
        # Settings not patched.
        st = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert "hooks" not in st

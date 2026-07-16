"""Tests for src/smriti/integrations/codex/install.py.

Idempotent TOML patching of ~/.codex/config.toml is the Codex-side
analog of the Claude Code JSON patching. Same risks: missed entries,
duplicates piling up on re-run, user keys clobbered.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from smriti.integrations.codex import install as codex_install
from smriti.integrations.common.hook_model import (
    EQUIVALENT,
    classify_wake_hook,
    make_wake_hook_command,
    parse_wake_command,
)
from smriti.integrations.common.managed_doc import (
    MANAGED_BEGIN,
    classify_doc,
)


@pytest.fixture
def fake_codex_home(tmp_path, monkeypatch):
    """Redirect every ~/.codex/* path the install module uses."""
    home = tmp_path
    codex_dir = home / ".codex"
    codex_dir.mkdir()

    monkeypatch.setattr(codex_install, "HOME", home)
    monkeypatch.setattr(codex_install, "CODEX", codex_dir)
    monkeypatch.setattr(codex_install, "CONFIG_TOML", codex_dir / "config.toml")
    monkeypatch.setattr(codex_install, "AGENTS_MD", codex_dir / "AGENTS.md")
    monkeypatch.setattr(codex_install, "HOOKS_DST", codex_dir / "hooks")
    return home


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --- patch_config_toml --------------------------------------------------

class TestPatchConfigTomlFromScratch:
    def test_writes_features_hooks_and_mcp(self, fake_codex_home):
        memory_root = fake_codex_home / ".narada"
        codex_install.patch_config_toml(memory_root)

        data = _read_toml(fake_codex_home / ".codex" / "config.toml")
        assert data["features"]["hooks"] is True
        assert "SessionStart" in data["hooks"]
        assert "PostToolUse" in data["hooks"]
        assert data["mcp_servers"]["smriti"] == {
            "command": "python",
            "args": ["-m", "smriti.mcp_server"],
        }

    def test_session_start_uses_codex_json_framing(self, fake_codex_home):
        codex_install.patch_config_toml(fake_codex_home / ".narada")
        data = _read_toml(fake_codex_home / ".codex" / "config.toml")
        ss_cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        # Parse semantically: the command form is platform-dependent
        # (sh on POSIX, PowerShell -EncodedCommand on Windows).
        spec = parse_wake_command(ss_cmd)
        assert spec is not None
        assert spec.env["SMRITI_WAKE_FRAMING"] == "codex-json"
        assert spec.env["SMRITI_WAKE_AUDIENCE"] == "coding"
        assert spec.wake_path.endswith(".smriti/wake.py")

    def test_post_tool_use_matcher_includes_apply_patch(self, fake_codex_home):
        codex_install.patch_config_toml(fake_codex_home / ".narada")
        data = _read_toml(fake_codex_home / ".codex" / "config.toml")
        post = data["hooks"]["PostToolUse"][0]
        # Codex's matcher accepts apply_patch's aliases (Edit, Write).
        assert "apply_patch" in post["matcher"]
        cmd = post["hooks"][0]["command"]
        assert 'SMRITI_RECALL_FRAMING="codex-json"' in cmd
        assert "recall_hook.py" in cmd

    def test_session_start_matcher_covers_context_resets(self, fake_codex_home):
        codex_install.patch_config_toml(fake_codex_home / ".narada")
        data = _read_toml(fake_codex_home / ".codex" / "config.toml")
        assert data["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"


class TestPatchConfigTomlMergeBehavior:
    def test_preserves_existing_user_keys(self, fake_codex_home):
        # Top-level bare keys must come BEFORE any [section] header — TOML
        # scoping drops them into the most recent section otherwise.
        config = fake_codex_home / ".codex" / "config.toml"
        config.write_text(
            'unrelated_top_level = 42\n\n'
            '[some_user_setting]\nkey = "value"\n',
            encoding="utf-8",
        )

        codex_install.patch_config_toml(fake_codex_home / ".narada")

        data = _read_toml(config)
        assert data["some_user_setting"]["key"] == "value"
        assert data["unrelated_top_level"] == 42
        # And our keys are still present.
        assert data["features"]["hooks"] is True

    def test_idempotent_re_run_no_duplicate_hooks(self, fake_codex_home, capsys):
        memory_root = fake_codex_home / ".narada"
        codex_install.patch_config_toml(memory_root)
        capsys.readouterr()

        codex_install.patch_config_toml(memory_root)
        out = capsys.readouterr().out
        assert "already wired" in out  # SessionStart
        # And no duplicates in the array.
        data = _read_toml(fake_codex_home / ".codex" / "config.toml")
        assert len(data["hooks"]["SessionStart"]) == 1
        assert len(data["hooks"]["PostToolUse"]) == 1

    def test_replaces_legacy_smriti_wake_hook(self, fake_codex_home):
        config = fake_codex_home / ".codex" / "config.toml"
        config.write_text(
            "[[hooks.SessionStart]]\n"
            'matcher = "startup|resume"\n'
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            'command = \'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"\'\n',
            encoding="utf-8",
        )

        codex_install.patch_config_toml(fake_codex_home / ".narada")

        data = _read_toml(config)
        hooks = data["hooks"]["SessionStart"]
        assert len(hooks) == 1
        assert hooks[0]["matcher"] == "startup|resume|clear|compact"
        verdict, detail = classify_wake_hook(
            hooks[0]["hooks"][0]["command"],
            memory_root=fake_codex_home / ".narada",
            home=fake_codex_home,
            framing="codex-json",
        )
        assert verdict == EQUIVALENT, detail

    def test_idempotent_when_mcp_already_registered(self, fake_codex_home, capsys):
        codex_install.patch_config_toml(fake_codex_home / ".narada")
        capsys.readouterr()
        codex_install.patch_config_toml(fake_codex_home / ".narada")
        out = capsys.readouterr().out
        assert "already registered" in out

    def test_creates_backup_before_overwriting(self, fake_codex_home):
        config = fake_codex_home / ".codex" / "config.toml"
        config.write_text("[orig]\nval = 1\n", encoding="utf-8")

        codex_install.patch_config_toml(fake_codex_home / ".narada")

        backup = config.with_suffix(".toml.bak")
        assert backup.exists()
        assert "[orig]" in backup.read_text(encoding="utf-8")

    def test_does_not_back_up_when_no_change(self, fake_codex_home):
        memory_root = fake_codex_home / ".narada"
        codex_install.patch_config_toml(memory_root)
        # Drop any backup from the first run.
        backup = (fake_codex_home / ".codex" / "config.toml.bak")
        if backup.exists():
            backup.unlink()

        # Re-run is a no-op: should not create another backup.
        codex_install.patch_config_toml(memory_root)
        assert not backup.exists()


# --- write_agents_md ----------------------------------------------------

class TestWriteAgentsMd:
    def test_writes_composed_doc_with_codex_addendum(self, fake_codex_home):
        codex_install.write_agents_md(fake_codex_home / ".narada")
        out = (fake_codex_home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")

        assert out.startswith("# AGENTS.md (user-global)")
        # AGENT.md body present.
        assert "## Memory system" in out
        # Codex-specific addendum present.
        assert "Session wake (Codex CLI)" in out
        # PostToolUse parity language present (added in Phase 8b).
        assert "ambient recall is wired" in out

    def test_size_under_codex_cap(self, fake_codex_home):
        """Codex truncates project-doc loading at 32 KiB."""
        codex_install.write_agents_md(fake_codex_home / ".narada")
        size = (fake_codex_home / ".codex" / "AGENTS.md").stat().st_size
        assert size < 32_000

    def test_idempotent_when_unchanged(self, fake_codex_home, capsys):
        codex_install.write_agents_md(fake_codex_home / ".narada")
        capsys.readouterr()
        codex_install.write_agents_md(fake_codex_home / ".narada")
        out = capsys.readouterr().out
        assert "up to date" in out


# --- run_codex orchestrator ---------------------------------------------

class TestRunCodex:
    def test_full_run_creates_all_artifacts(self, fake_codex_home):
        assert codex_install.run_codex(fake_codex_home / ".narada") is True
        codex = fake_codex_home / ".codex"
        assert (codex / "config.toml").exists()
        assert (codex / "AGENTS.md").exists()
        assert (codex / "hooks" / "recall_hook.py").exists()

    def test_skip_config_only_writes_agents_md_and_hooks(self, fake_codex_home):
        codex_install.run_codex(fake_codex_home / ".narada", skip_config=True)
        codex = fake_codex_home / ".codex"
        assert not (codex / "config.toml").exists()
        assert (codex / "AGENTS.md").exists()
        # Hooks still deployed.
        assert (codex / "hooks" / "recall_hook.py").exists()

    def test_idempotent_full_run(self, fake_codex_home):
        codex_install.run_codex(fake_codex_home / ".narada")
        # Snapshot config + AGENTS.md content.
        config_before = (fake_codex_home / ".codex" / "config.toml").read_bytes()
        agents_before = (fake_codex_home / ".codex" / "AGENTS.md").read_bytes()

        codex_install.run_codex(fake_codex_home / ".narada")

        assert (fake_codex_home / ".codex" / "config.toml").read_bytes() == config_before
        assert (fake_codex_home / ".codex" / "AGENTS.md").read_bytes() == agents_before


# --- agent-doc preflight (adversarial review findings 1 + 5) --------------

class TestAgentDocPreflight:
    def test_unmarked_agents_md_refuses_before_any_mutation(self, fake_codex_home):
        """Fail-closed: a legacy AGENTS.md must abort the install BEFORE
        config.toml or hooks are touched, and run_codex must report
        failure (not exit-0 success)."""
        agents = fake_codex_home / ".codex" / "AGENTS.md"
        agents.write_text("# Legacy generated doc, no markers\n", encoding="utf-8")

        ok = codex_install.run_codex(fake_codex_home / ".narada")

        assert ok is False
        # Nothing else was mutated.
        assert not (fake_codex_home / ".codex" / "config.toml").exists()
        assert not (fake_codex_home / ".codex" / "hooks").exists()
        # And the doc itself is untouched.
        assert agents.read_text(encoding="utf-8") == "# Legacy generated doc, no markers\n"

    def test_migrate_doc_flag_converts_then_proceeds(self, fake_codex_home):
        agents = fake_codex_home / ".codex" / "AGENTS.md"
        agents.write_text(
            "# AGENTS.md (user-global)\n\n"
            "## Memory system — smriti is the single write path\n\nold text\n",
            encoding="utf-8",
        )

        ok = codex_install.run_codex(fake_codex_home / ".narada", migrate_doc=True)

        assert ok is True
        assert classify_doc(agents).kind == "managed"
        assert agents.with_name("AGENTS.md.pre-migrate.bak").exists()
        assert (fake_codex_home / ".codex" / "config.toml").exists()

    def test_user_content_outside_block_survives_rerun(self, fake_codex_home):
        assert codex_install.run_codex(fake_codex_home / ".narada") is True
        agents = fake_codex_home / ".codex" / "AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        agents.write_text(text + "\n## My own Codex notes\n\nkeep me\n", encoding="utf-8")

        assert codex_install.run_codex(fake_codex_home / ".narada") is True
        assert "keep me" in agents.read_text(encoding="utf-8")


class TestLivePowerShellHookPreserved:
    def test_encoded_equivalent_hook_is_left_untouched(self, fake_codex_home):
        """Never regress a working hook: the live Windows install uses a
        PowerShell -EncodedCommand form; a re-run must classify it
        equivalent and leave the bytes alone."""
        memory_root = fake_codex_home / ".narada"
        ps_cmd = make_wake_hook_command(
            memory_root,
            home=fake_codex_home,
            framing="codex-json",
            style="powershell-encoded",
        )
        config = fake_codex_home / ".codex" / "config.toml"
        config.write_text(
            "[features]\nhooks = true\n\n"
            "[[hooks.SessionStart]]\n"
            'matcher = "startup|resume|clear|compact"\n'
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            f"command = '{ps_cmd}'\n",
            encoding="utf-8",
        )

        codex_install.patch_config_toml(memory_root)

        data = _read_toml(config)
        hooks = data["hooks"]["SessionStart"]
        assert len(hooks) == 1
        assert hooks[0]["hooks"][0]["command"] == ps_cmd

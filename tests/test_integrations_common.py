"""Tests for src/smriti/integrations/common/.

This is the single-point-of-change abstraction shared by every
harness adapter. Pinning its behavior here means a Claude Code or
Codex regression caused by a common-module change is caught
immediately.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from smriti.integrations.common import (
    SMRITI_MCP_COMMAND,
    compose_agent_doc,
    deploy_hook_scripts,
    make_wake_hook_command,
)
from smriti.integrations.common import wake_runner
from smriti.integrations.common.agent_md import AGENT_MD_PATH


# --- mcp_spec ------------------------------------------------------------

class TestMCPSpec:
    def test_command_constant_shape(self):
        assert SMRITI_MCP_COMMAND == {
            "command": "python",
            "args": ["-m", "smriti.mcp_server"],
        }

    def test_make_wake_hook_command_raw(self):
        cmd = make_wake_hook_command(
            Path("/home/u/.narada"), home=Path("/home/u"), framing="raw",
        )
        # Raw framing has no SMRITI_WAKE_FRAMING var.
        assert "SMRITI_WAKE_FRAMING" not in cmd
        assert "SMRITI_WAKE=1" in cmd
        assert 'SMRITI_ROOT="$HOME/.narada"' in cmd
        assert '"$HOME/.narada/.smriti/wake.py"' in cmd
        # Forward-slash paths only — bash hostility to backslashes.
        assert "\\" not in cmd

    def test_make_wake_hook_command_codex_json(self):
        cmd = make_wake_hook_command(
            Path("/home/u/.narada"), home=Path("/home/u"), framing="codex-json",
        )
        assert 'SMRITI_WAKE_FRAMING="codex-json"' in cmd
        assert "SMRITI_WAKE=1" in cmd

    def test_make_wake_hook_command_unknown_framing(self):
        with pytest.raises(ValueError):
            make_wake_hook_command(
                Path("/home/u/.narada"), home=Path("/home/u"),
                framing="bogus",
            )

    def test_make_wake_hook_command_uses_relative_path_under_home(self):
        cmd = make_wake_hook_command(
            Path("/home/u/.tara"), home=Path("/home/u"), framing="raw",
        )
        assert "$HOME/.tara" in cmd
        assert "/home/u" not in cmd  # absolute path not leaked


# --- agent_md ------------------------------------------------------------

class TestAgentMdComposition:
    def test_template_is_packaged(self):
        assert AGENT_MD_PATH.exists()
        assert AGENT_MD_PATH.parent.name == "templates"

    def test_compose_strips_preamble(self):
        out = compose_agent_doc(
            addendum="## Harness X\nfoo\n",
            memory_rel="~/.narada",
            header="# CLAUDE.md (test)",
        )
        # Header replaces AGENT.md's own title + intro.
        assert out.startswith("# CLAUDE.md (test)\n\n")
        # The AGENT.md preamble explanation about the template should NOT
        # appear in the composed harness doc.
        assert "Per-harness adapters wrap or include" not in out
        # First H2 from AGENT.md should be present.
        assert "## Memory system" in out

    def test_compose_appends_addendum(self):
        out = compose_agent_doc(
            addendum="## Hook system\nUnique-marker-12345\n",
            memory_rel="~/.narada",
            header="# CLAUDE.md (test)",
        )
        assert "Unique-marker-12345" in out
        # Addendum lands at the end.
        assert out.rstrip().endswith("Unique-marker-12345")

    def test_compose_substitutes_memory_rel(self):
        out = compose_agent_doc(
            addendum="addendum",
            memory_rel="~/.tara-special",
            header="# H",
        )
        assert "~/.tara-special" in out
        # The AGENT.md template uses `{{name}}` for literal braces; those
        # should now show as `{name}` after format() resolution.
        assert "{name}" in out
        assert "{{name}}" not in out

    def test_compose_does_not_double_format_addendum(self):
        # If the addendum has its own {memory_rel} placeholder it should
        # also be substituted (caller controls this; documenting current
        # behavior).
        out = compose_agent_doc(
            addendum="path: {memory_rel}/foo",
            memory_rel="~/.narada",
            header="# H",
        )
        assert "path: ~/.narada/foo" in out


# --- wake_runner ---------------------------------------------------------

class TestWakeRunnerFraming:
    """Codex-json framing has zero coverage outside the live install
    smoke-test. Pin the JSON shape here so Codex's hookSpecificOutput
    contract is enforced."""

    def _drive(self, monkeypatch, framing):
        # Stub out briefing so we don't depend on a live memory tree.
        import smriti.wake as _wake_pkg

        def fake_briefing(memory_root=None, cwd=None, **kw):
            return "<<test briefing payload>>"

        monkeypatch.setattr(_wake_pkg, "briefing", fake_briefing)
        monkeypatch.setenv("SMRITI_WAKE", "1")
        if framing is not None:
            monkeypatch.setenv("SMRITI_WAKE_FRAMING", framing)
        else:
            monkeypatch.delenv("SMRITI_WAKE_FRAMING", raising=False)

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = wake_runner.run()
        assert rc == 0
        return buf.getvalue()

    def test_raw_framing_default(self, monkeypatch):
        out = self._drive(monkeypatch, framing=None)
        assert out == "<<test briefing payload>>"

    def test_raw_framing_explicit(self, monkeypatch):
        out = self._drive(monkeypatch, framing="raw")
        assert out == "<<test briefing payload>>"

    def test_codex_json_framing_shape(self, monkeypatch):
        out = self._drive(monkeypatch, framing="codex-json")
        parsed = json.loads(out)
        assert list(parsed.keys()) == ["hookSpecificOutput"]
        inner = parsed["hookSpecificOutput"]
        assert inner["hookEventName"] == "SessionStart"
        assert inner["additionalContext"] == "<<test briefing payload>>"

    def test_silent_when_wake_unset(self, monkeypatch):
        monkeypatch.delenv("SMRITI_WAKE", raising=False)
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = wake_runner.run()
        assert rc == 0
        assert buf.getvalue() == ""

    def test_unknown_framing_raises(self, monkeypatch):
        monkeypatch.setenv("SMRITI_WAKE", "1")
        monkeypatch.setenv("SMRITI_WAKE_FRAMING", "totally-bogus")
        with pytest.raises(ValueError, match="unknown framing"):
            wake_runner.run()


# --- hook_scripts --------------------------------------------------------

class TestDeployHookScripts:
    def test_fresh_deploy_copies(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("# a", encoding="utf-8")
        (src / "b.py").write_text("# b", encoding="utf-8")
        dst = tmp_path / "dst"

        written = deploy_hook_scripts(src, dst, ["a.py", "b.py"])
        assert sorted(p.name for p in written) == ["a.py", "b.py"]
        assert (dst / "a.py").read_text(encoding="utf-8") == "# a"

    def test_idempotent_when_unchanged(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "h.py").write_text("# h", encoding="utf-8")
        dst = tmp_path / "dst"

        deploy_hook_scripts(src, dst, ["h.py"])
        # Second run: no writes since bytes match.
        written = deploy_hook_scripts(src, dst, ["h.py"])
        assert written == []

    def test_overwrites_when_drifted(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "h.py").write_text("new content", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "h.py").write_text("stale content", encoding="utf-8")

        written = deploy_hook_scripts(src, dst, ["h.py"])
        assert len(written) == 1
        assert (dst / "h.py").read_text(encoding="utf-8") == "new content"

    def test_missing_source_skipped(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        written = deploy_hook_scripts(src, dst, ["does_not_exist.py"])
        assert written == []
        out = capsys.readouterr().out
        assert "source missing" in out

    def test_creates_dst_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.py").write_text("# x", encoding="utf-8")
        dst = tmp_path / "deeply" / "nested" / "dst"
        assert not dst.exists()
        deploy_hook_scripts(src, dst, ["x.py"])
        assert (dst / "x.py").exists()

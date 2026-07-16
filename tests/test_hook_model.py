"""Tests for src/smriti/integrations/common/hook_model.py.

The canonical wake-hook model is shared by installers and doctor —
generator and classifier live together precisely so these tests can
pin the roundtrip: whatever any style generates must classify
EQUIVALENT, and the live PowerShell form found on Windows machines
must never be misread as broken (the 2026-07-16 audit's false-FAIL).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from smriti.integrations.common.hook_model import (
    DEFICIENT,
    EQUIVALENT,
    UNRELATED,
    classify_wake_hook,
    make_wake_hook_command,
    parse_wake_command,
)

HOME = Path("/home/u")
ROOT = HOME / ".narada"


def _classify(cmd: str, framing: str = "raw") -> tuple[str, str]:
    return classify_wake_hook(cmd, memory_root=ROOT, home=HOME, framing=framing)


# The exact command shape found live in ~/.codex/config.toml on the
# Windows machine during the 2026-07-16 audit (regenerated here so the
# test is entity-name-agnostic).
def _live_style_ps_command() -> str:
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "$ErrorActionPreference='Stop'; "
        "$env:SMRITI_WAKE='1'; "
        "$env:SMRITI_ROOT=(Join-Path $HOME '.narada'); "
        "$env:SMRITI_WAKE_AUDIENCE='coding'; "
        "$env:SMRITI_WAKE_FRAMING='codex-json'; "
        "python (Join-Path $HOME '.narada/.smriti/wake.py')"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


class TestGeneration:
    def test_sh_form_shape(self):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing="raw", style="sh")
        assert "SMRITI_WAKE=1" in cmd
        assert 'SMRITI_ROOT="$HOME/.narada"' in cmd
        assert "\\" not in cmd

    def test_powershell_form_is_encoded(self):
        cmd = make_wake_hook_command(
            ROOT, home=HOME, framing="codex-json", style="powershell-encoded"
        )
        assert cmd.startswith("powershell.exe -NoProfile -NonInteractive -EncodedCommand ")
        # No raw env assignments leak into the outer command.
        assert "SMRITI_WAKE=1" not in cmd

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError, match="style"):
            make_wake_hook_command(ROOT, home=HOME, style="cmd-exe")

    def test_unknown_framing_raises(self):
        with pytest.raises(ValueError, match="framing"):
            make_wake_hook_command(ROOT, home=HOME, framing="bogus")


class TestParse:
    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    @pytest.mark.parametrize("framing", ["raw", "codex-json"])
    def test_roundtrip_parse(self, style, framing):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing=framing, style=style)
        spec = parse_wake_command(cmd)
        assert spec is not None
        assert spec.wake_path == ".narada/.smriti/wake.py"
        assert spec.env["SMRITI_WAKE"] == "1"
        assert spec.env.get("SMRITI_WAKE_AUDIENCE") == "coding"
        if framing == "codex-json":
            assert spec.env["SMRITI_WAKE_FRAMING"] == "codex-json"

    def test_non_wake_command_returns_none(self):
        assert parse_wake_command("python ~/.claude/hooks/recall.py") is None
        assert parse_wake_command("echo hello") is None

    def test_garbage_encoded_returns_none(self):
        assert parse_wake_command("powershell.exe -EncodedCommand AAAA") is None


class TestClassify:
    @pytest.mark.parametrize("style", ["sh", "powershell-encoded"])
    @pytest.mark.parametrize("framing", ["raw", "codex-json"])
    def test_generated_commands_classify_equivalent(self, style, framing):
        cmd = make_wake_hook_command(ROOT, home=HOME, framing=framing, style=style)
        verdict, detail = _classify(cmd, framing=framing)
        assert verdict == EQUIVALENT, detail

    def test_live_windows_ps_hook_is_equivalent(self):
        """The 2026-07-16 audit false-FAIL: a working PowerShell hook
        must classify equivalent, not broken."""
        verdict, detail = _classify(_live_style_ps_command(), framing="codex-json")
        assert verdict == EQUIVALENT, detail

    def test_missing_framing_is_deficient_for_codex(self):
        legacy = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(legacy, framing="codex-json")
        assert verdict == DEFICIENT
        assert "framing" in detail

    def test_missing_audience_defaults_to_coding_equivalent(self):
        """The live Claude Code hook predates the audience var; the
        runner defaults to coding, so it is semantically equivalent."""
        legacy = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(legacy, framing="raw")
        assert verdict == EQUIVALENT, detail

    def test_missing_smriti_wake_is_deficient(self):
        cmd = 'SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"'
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert "SMRITI_WAKE" in detail

    def test_wrong_root_is_deficient(self):
        cmd = (
            'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.other" '
            'python "$HOME/.narada/.smriti/wake.py"'
        )
        verdict, detail = _classify(cmd)
        assert verdict == DEFICIENT
        assert "SMRITI_ROOT" in detail

    def test_other_entity_wake_is_unrelated(self):
        cmd = 'SMRITI_WAKE=1 python "$HOME/.tara/.smriti/wake.py"'
        verdict, _ = _classify(cmd)
        assert verdict == UNRELATED

    def test_non_wake_hook_is_unrelated(self):
        verdict, _ = _classify("python ~/.claude/hooks/associative_recall.py")
        assert verdict == UNRELATED

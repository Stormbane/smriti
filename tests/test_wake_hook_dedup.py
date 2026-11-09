"""Regression tests for the diff-review P2s (2026-07-17).

P2-2: mixed legacy/current hook configurations must never end up with
two active smriti wake hooks — the briefing would load twice per
session. Exactly one survives: the first equivalent, else one upgraded
deficient. Applies to both harnesses.
"""

from __future__ import annotations

import base64
import json
import tomllib

import pytest

from smriti.integrations.claude_code import install as cc_install
from smriti.integrations.codex import install as codex_install
from smriti.integrations.common.hook_model import (
    DEFICIENT,
    EQUIVALENT,
    classify_wake_hook,
    installing_interpreter,
    make_wake_hook_command,
    parse_wake_command,
)
from smriti.integrations.common.mcp_spec import registered
from tests._home import use_home


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    return use_home(monkeypatch, tmp_path)


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
        home = use_home(monkeypatch, tmp_path)
        codex_dir = home / ".codex"
        codex_dir.mkdir()
        memory_root = home / ".narada"
        eq = make_wake_hook_command(memory_root, home=home, framing="codex-json")
        config = codex_dir / "config.toml"
        config.write_text(
            "[features]\nhooks = true\n\n"
            "[[hooks.SessionStart]]\n"
            'matcher = "startup|resume|clear|compact"\n'
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            f"command = {json.dumps(eq)}\n"  # a basic string: the command holds quotes
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            f"command = {json.dumps(eq)}\n",
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


# H15.1: the hook runs the python that installed it. One that runs `python`
# from PATH (as every install before H15.1 wrote it) is brought up to date.

def _legacy_command(framing: str, style: str) -> str:
    """The wake hook as installers wrote it before H15.1: a bare ``python``."""
    if style == "sh":
        framing_var = f'SMRITI_WAKE_FRAMING="{framing}" ' if framing != "raw" else ""
        return ('SMRITI_WAKE=1 SMRITI_ROOT="$HOME/.narada" SMRITI_WAKE_AUDIENCE="coding" '
                f'{framing_var}python "$HOME/.narada/.smriti/wake.py"')
    script = ("$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; "
              "$env:SMRITI_WAKE='1'; $env:SMRITI_ROOT=(Join-Path $HOME '.narada'); "
              "$env:SMRITI_WAKE_AUDIENCE='coding'; "
              f"$env:SMRITI_WAKE_FRAMING='{framing}'; "
              "python (Join-Path $HOME '.narada/.smriti/wake.py')")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


ACTIVITY = 'touch "$HOME/.narada/.smriti/last-activity" 2>/dev/null || true'


@pytest.mark.parametrize("activity_wired", [False, True], ids=["alone", "activity-wired"])
@pytest.mark.parametrize("legacy", [
    _legacy_command("raw", "sh"),
    'SMRITI_ROOT="$HOME/.narada" python "$HOME/.narada/.smriti/wake.py"',  # no SMRITI_WAKE too
], ids=["python-from-path", "no-smriti-wake"])
def test_claude_upgrades_a_deficient_hook_on_disk(fake_home, legacy, activity_wired):
    """The upgrade must reach the file. It rewrote the hook in place, so the
    rebuilt SessionStart list compared equal to the one it was built from;
    with the activity hook already wired nothing else changed, and the
    upgrade was reported but never saved. Since the hook must name its
    interpreter, that is every existing install."""
    memory_root = fake_home / ".narada"
    settings: dict = {"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": legacy}]},
    ]}}
    if activity_wired:
        settings["hooks"]["UserPromptSubmit"] = [
            {"matcher": "", "hooks": [{"type": "command", "command": ACTIVITY}]}]
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    verdict, detail = classify_wake_hook(legacy, memory_root=memory_root, home=fake_home)
    assert verdict == DEFICIENT, detail

    assert cc_install.patch_settings_json(memory_root) is True

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    [wake] = _wake_hooks_in(data, memory_root, fake_home)
    assert wake == make_wake_hook_command(memory_root, home=fake_home, framing="raw")
    spec = parse_wake_command(wake)
    assert spec is not None
    assert spec.interpreter.replace("\\", "/") == installing_interpreter().replace("\\", "/")
    activity = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert activity == [ACTIVITY]
    # And a rerun finds nothing left to do.
    before = settings_path.read_bytes()
    cc_install.patch_settings_json(memory_root)
    assert settings_path.read_bytes() == before


def test_codex_upgrades_a_hook_that_runs_python_from_path(tmp_path, monkeypatch):
    home = use_home(monkeypatch, tmp_path)
    (home / ".codex").mkdir()
    memory_root = home / ".narada"
    style = codex_install._wake_style()
    legacy = _legacy_command("codex-json", style)
    config = home / ".codex" / "config.toml"
    # Hooks enabled and the server registered already: the wake hook is the
    # only change, so its upgrade alone must be detected and saved.
    mcp = registered(None, memory_root)
    config.write_text(
        "[features]\nhooks = true\n\n"
        "[[hooks.SessionStart]]\n"
        'matcher = "startup|resume|clear|compact"\n'
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(legacy)}\n\n"
        "[mcp_servers.smriti]\n"
        f"command = {json.dumps(mcp['command'])}\n"
        f"args = {json.dumps(mcp['args'])}\n\n"
        "[mcp_servers.smriti.env]\n"
        f"SMRITI_ROOT = {json.dumps(mcp['env']['SMRITI_ROOT'])}\n",
        encoding="utf-8",
    )
    verdict, detail = classify_wake_hook(legacy, memory_root=memory_root, home=home,
                                         framing="codex-json")
    assert verdict == DEFICIENT and "python from PATH" in detail
    assert codex_install._wire_config(tomllib.loads(config.read_text(encoding="utf-8")),
                                      [], memory_root, home) is True  # the hook alone

    assert codex_install.patch_config_toml(memory_root) is True

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    wake_cmds = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert wake_cmds == [make_wake_hook_command(memory_root, home=home, framing="codex-json",
                                                style=style)]
    # And a rerun finds nothing left to do.
    assert codex_install._wire_config(data, [], memory_root, home) is False

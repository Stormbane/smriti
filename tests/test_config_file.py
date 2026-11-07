"""H13.5: the installer edits a harness's config without losing what anyone else wrote.

``~/.claude.json`` and ``~/.claude/settings.json`` (and Codex's
``config.toml``) are rewritten by the harness while it runs. Outside review
#6 found the installer reading, changing and rewriting them with a plain
``write_text``, no lock, a single ``.bak`` overwritten on every run, and the
home fixed when the module was imported. These run against a temporary home.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
import types
from pathlib import Path

import pytest

from smriti.core.files import FileBusy, edit_lock
from smriti.integrations.claude_code import install as cc_install
from smriti.integrations.codex import install as codex_install
from smriti.integrations.common import config_file
from smriti.integrations.common.config_file import BACKUPS, backups, edit_config
from tests._home import use_home

ORIGINAL = {"userID": "sam-0001", "projects": {"/work/ari": {"allowedTools": []}},
            "mcpServers": {"other": {"command": "node", "args": ["other.js"]}}}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".claude").mkdir()
    return use_home(monkeypatch, tmp_path)


def _json(path: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _unregister(config: Path) -> None:
    """Claude Code, or the user, takes smriti's server out: the next install puts it back."""
    data = _json(config)
    servers = data["mcpServers"]
    assert isinstance(servers, dict)
    servers.pop("smriti", None)
    config.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- backups


def test_a_second_install_keeps_the_original_backup(home: Path) -> None:
    config = home / ".claude.json"
    config.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    assert cc_install.register_mcp_server() is True
    [first] = backups(config)
    assert _json(first) == ORIGINAL

    _unregister(config)
    assert cc_install.register_mcp_server() is True
    later = backups(config)
    assert len(later) == 2 and later[0] == first and _json(first) == ORIGINAL
    assert "smriti" not in _json(later[1])["mcpServers"]  # type: ignore[operator]

    for _ in range(BACKUPS + 3):  # many installs: the first backup stays, the rest are capped
        _unregister(config)
        assert cc_install.register_mcp_server() is True
    kept = backups(config)
    assert len(kept) == BACKUPS and kept[0] == first and _json(first) == ORIGINAL


def test_settings_json_keeps_its_original_backup_too(home: Path) -> None:
    settings = home / ".claude" / "settings.json"
    original = {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"command": "echo bye"}]}]}}
    settings.write_text(json.dumps(original), encoding="utf-8")
    memory_root = home / ".ari"
    assert cc_install.patch_settings_json(memory_root) is True
    data = _json(settings)
    assert data["theme"] == "dark" and "SessionStart" in data["hooks"]  # type: ignore[operator]

    data["hooks"] = {}  # the user cleared the hooks; a re-install wires them again
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert cc_install.patch_settings_json(memory_root) is True
    first, second = backups(settings)
    assert _json(first) == original and _json(second)["hooks"] == {}


def test_codex_config_keeps_its_original_backup(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    use_home(monkeypatch, tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "o-sam"\n', encoding="utf-8")
    assert codex_install.patch_config_toml(tmp_path / ".ari") is True
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    del data["mcp_servers"]
    config.write_text('model = "o-sam"\n[features]\nhooks = true\n', encoding="utf-8")
    assert codex_install.patch_config_toml(tmp_path / ".ari") is True
    first, _second = backups(config)
    assert first.read_text(encoding="utf-8") == 'model = "o-sam"\n'


def test_no_change_writes_nothing_and_backs_up_nothing(home: Path) -> None:
    config = home / ".claude.json"
    config.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    cc_install.register_mcp_server()
    written = config.read_bytes()
    cc_install.register_mcp_server()
    assert config.read_bytes() == written and len(backups(config)) == 1


# ---------------------------------------------------------------- concurrent writers


def _harness_writes_during_the_edit(monkeypatch: pytest.MonkeyPatch, config: Path,
                                    times: int) -> list[int]:
    """Claude Code rewrites the file while the install is changing it, *times* times."""
    real = cc_install.mcp_registration_matches
    calls: list[int] = []

    def matches(entry: object) -> bool:
        calls.append(1)
        if len(calls) <= times:
            data = _json(config)
            data[f"written_by_claude_code_{len(calls)}"] = True
            config.write_text(json.dumps(data), encoding="utf-8")
        return real(entry)

    monkeypatch.setattr(cc_install, "mcp_registration_matches", matches)
    return calls


def test_a_change_between_read_and_write_is_not_clobbered(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = home / ".claude.json"
    config.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    calls = _harness_writes_during_the_edit(monkeypatch, config, times=1)

    assert cc_install.register_mcp_server() is True
    data = _json(config)
    assert data["written_by_claude_code_1"] is True  # Claude Code's write kept
    assert "smriti" in data["mcpServers"]  # type: ignore[operator]
    assert len(calls) == 2  # read again and redone on Claude Code's text
    # The backup is the file the change was made to: Claude Code's version.
    [backup] = backups(config)
    assert _json(backup)["written_by_claude_code_1"] is True
    assert capsys.readouterr().out.count("registered smriti server") == 1


def test_a_file_that_keeps_changing_is_left_as_the_harness_wrote_it(
        home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = home / ".claude.json"
    config.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    _harness_writes_during_the_edit(monkeypatch, config, times=2)

    assert cc_install.run_claude_code(home / ".ari") is False
    data = _json(config)
    assert data["written_by_claude_code_2"] is True and "smriti" not in data["mcpServers"]  # type: ignore[operator]
    assert backups(config) == []
    assert "NOT SAVED" in capsys.readouterr().out
    # The other file's edit went ahead, and a later install (Claude Code quiet) finishes.
    assert "SessionStart" in _json(home / ".claude" / "settings.json")["hooks"]  # type: ignore[operator]
    assert cc_install.run_claude_code(home / ".ari") is True
    assert "smriti" in _json(config)["mcpServers"]  # type: ignore[operator]


def test_the_edit_holds_the_files_lock_from_read_to_write(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    seen: list[bool] = []

    def change(text: str | None) -> str | None:
        try:
            with edit_lock(path, wait_s=0.05):
                seen.append(False)
        except FileBusy:
            seen.append(True)  # another installer would wait here
        return '{"a": 1}'

    assert edit_config(path, change).changed
    assert seen == [True]


def test_a_missing_file_is_created_without_a_backup(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    done = edit_config(path, lambda text: "a = 1\n" if text is None else None)
    assert done.changed and done.backup is None
    assert path.read_text(encoding="utf-8") == "a = 1\n" and backups(path) == []


# ---------------------------------------------------------------- the home, the file


def test_the_home_is_read_when_a_step_runs_not_at_import(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("first", "second"):
        home = use_home(monkeypatch, tmp_path / name)
        home.mkdir()
        (home / ".claude.json").write_text("{}", encoding="utf-8")
        assert cc_install.claude_config_path() == home / ".claude.json"
        assert cc_install.register_mcp_server() is True
        assert "smriti" in _json(home / ".claude.json")["mcpServers"]  # type: ignore[operator]
    assert codex_install.config_toml_path() == tmp_path / "second" / ".codex" / "config.toml"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows relies on ACLs")
def test_a_private_file_stays_private(home: Path) -> None:
    config = home / ".claude.json"
    config.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    os.chmod(config, 0o600)
    assert cc_install.register_mcp_server() is True
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    [backup] = backups(config)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_a_linked_config_stays_a_link(home: Path) -> None:
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "claude.json"
    target.write_text(json.dumps(ORIGINAL), encoding="utf-8")
    link = home / ".claude.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("no symbolic links here (Windows without the privilege)")
    assert cc_install.register_mcp_server() is True
    assert link.is_symlink()
    assert "smriti" in _json(target)["mcpServers"]  # type: ignore[operator]
    assert len(backups(target)) == 1


def test_a_backup_that_fails_part_way_is_removed_and_the_file_left(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A part-written backup (a full disk) would otherwise be kept for ever as the first."""
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "dark"}', encoding="utf-8")
    real_fdopen = os.fdopen

    class Full:
        def __init__(self, fd: int) -> None:
            self.f = real_fdopen(fd, "wb")

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.f.close()

        def write(self, data: bytes) -> int:
            self.f.write(data[:3])
            raise OSError(28, "No space left on device")

    # config_file's own os, so the locks and everything else keep the real one.
    proxy = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    proxy.fdopen = lambda fd, mode: Full(fd)
    monkeypatch.setattr(config_file, "os", proxy)
    done = edit_config(path, lambda text: '{"theme": "light"}')
    assert not done.changed and "No space left" in done.refused
    assert backups(path) == [] and path.read_text(encoding="utf-8") == '{"theme": "dark"}'


def test_the_backups_are_pruned_oldest_kept(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("v0", encoding="utf-8")
    for n in range(1, 9):
        assert edit_config(path, lambda text, n=n: f"v{n}").changed
    kept = backups(path)
    assert len(kept) == BACKUPS
    assert [b.read_text(encoding="utf-8") for b in kept] == ["v0", "v4", "v5", "v6", "v7"]
    assert config_file.BACKUPS == 5

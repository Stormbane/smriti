"""tell_suti: the MCP door to notify_suti.py. Nothing here sends a real message."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from smriti import mcp_server
from smriti.daylog import notify
from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.notify import Delivery, NotifyError, parse_channels, send


@pytest.fixture(autouse=True)
def _no_real_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any path that would reach the real notify_suti.py fails the test."""

    def _refuse(text: str, level: str) -> Delivery:
        raise AssertionError("tell_suti reached the real sender")

    monkeypatch.setattr(mcp_server, "_default_tell_suti_sender", _refuse)
    monkeypatch.setattr(mcp_server, "TELL_SUTI_SENDER", None)


class _Recorder:
    def __init__(self, channels: tuple[str, ...] = ("telegram", "app-push")) -> None:
        self.calls: list[tuple[str, str]] = []
        self.channels = channels

    def __call__(self, text: str, level: str) -> Delivery:
        self.calls.append((text, level))
        return Delivery(channels=self.channels, detail="fake")


def _call(arguments: dict) -> dict:
    response = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "tell_suti", "arguments": arguments},
    })
    assert response is not None
    return response["result"]


class TestTellSutiTool:
    def test_listed_with_schema(self) -> None:
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        assert "tell_suti" in tools
        schema = tools["tell_suti"]["inputSchema"]
        assert schema["required"] == ["text"]
        assert schema["properties"]["level"]["default"] == "message"

    def test_uses_injected_sender_and_reports_channels(self) -> None:
        rec = _Recorder()
        out = mcp_server.handle_tell_suti({"text": "  test  "}, sender=rec)
        assert rec.calls == [("test", "message")]
        assert out == "Delivered via telegram, app-push (level=message)."

    def test_level_passes_through(self) -> None:
        rec = _Recorder(channels=("app-push",))
        out = mcp_server.handle_tell_suti({"text": "disk full", "level": "ERROR"}, sender=rec)
        assert rec.calls == [("disk full", "error")]
        assert "app-push" in out

    def test_dispatch_uses_module_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(mcp_server, "TELL_SUTI_SENDER", rec)
        result = _call({"text": "hello from a session"})
        assert not result.get("isError")
        assert "telegram" in result["content"][0]["text"]
        assert rec.calls == [("hello from a session", "message")]

    @pytest.mark.parametrize("arguments", [
        {"text": "   "},
        {"text": "x", "level": "shout"},
        {"text": "x" * (notify.MAX_MESSAGE_CHARS + 1)},
    ])
    def test_bad_arguments_are_errors_and_send_nothing(
        self, monkeypatch: pytest.MonkeyPatch, arguments: dict,
    ) -> None:
        rec = _Recorder()
        monkeypatch.setattr(mcp_server, "TELL_SUTI_SENDER", rec)
        result = _call(arguments)
        assert result["isError"] is True
        assert rec.calls == []

    def test_nothing_delivered_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "TELL_SUTI_SENDER", _Recorder(channels=()))
        result = _call({"text": "anyone there?"})
        assert result["isError"] is True
        assert "not delivered" in result["content"][0]["text"]

    def test_sender_failure_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(text: str, level: str) -> Delivery:
            raise NotifyError("not delivered (exit 1): telegram: error")

        monkeypatch.setattr(mcp_server, "TELL_SUTI_SENDER", _boom)
        result = _call({"text": "hi"})
        assert result["isError"] is True
        assert "exit 1" in result["content"][0]["text"]


# ── send(): the real subprocess path, against a fake notify script ──────

_FAKE_SENDER = '''
import json, os, sys
from pathlib import Path
msg = sys.stdin.read()
Path(sys.argv[1]).write_text(json.dumps({
    "msg": msg, "level": os.environ.get("NOTIFY_LEVEL"),
    "source": os.environ.get("NOTIFY_SOURCE")}), encoding="utf-8")
print(os.environ.get("FAKE_REPORT", "delivered via: telegram, app-push"), file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


@pytest.fixture()
def fake_cfg(tmp_path: Path) -> tuple[DaylogConfig, Path]:
    script = tmp_path / "fake_notify.py"
    script.write_text(_FAKE_SENDER, encoding="utf-8")
    seen = tmp_path / "seen.json"
    return DaylogConfig(root=tmp_path, notify_cmd=[sys.executable, str(script), str(seen)]), seen


class TestSend:
    def test_pipes_text_with_level_and_source(self, fake_cfg) -> None:
        cfg, seen = fake_cfg
        delivery = send(cfg, "Ünïcode — test", level="error")
        assert delivery.channels == ("telegram", "app-push")
        got = json.loads(seen.read_text(encoding="utf-8"))
        assert got == {"msg": "Ünïcode — test", "level": "error", "source": "tell_suti"}

    def test_nonzero_exit_raises(self, fake_cfg, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg, _ = fake_cfg
        monkeypatch.setenv("FAKE_EXIT", "1")
        monkeypatch.setenv("FAKE_REPORT", "delivered via: NOTHING")
        with pytest.raises(NotifyError, match="exit 1"):
            send(cfg, "hi")

    @pytest.mark.parametrize("level", ["log", "warn"])
    def test_feed_only_level_refused_by_level_blind_command(
        self, tmp_path: Path, level: str,
    ) -> None:
        script = tmp_path / "notify_telegram.py"
        script.write_text(_FAKE_SENDER, encoding="utf-8")
        seen = tmp_path / "seen.json"
        cfg = DaylogConfig(root=tmp_path, notify_cmd=[sys.executable, str(script), str(seen)])
        with pytest.raises(NotifyError, match="feed-only"):
            send(cfg, "quiet note", level=level)
        assert not seen.exists()  # refused before the command ran

    def test_feed_only_level_passes_to_level_aware_sender(self, tmp_path: Path) -> None:
        script = tmp_path / "notify_suti.py"
        script.write_text(_FAKE_SENDER, encoding="utf-8")
        seen = tmp_path / "seen.json"
        cfg = DaylogConfig(root=tmp_path, notify_cmd=[sys.executable, str(script), str(seen)])
        send(cfg, "quiet note", level="log")
        assert json.loads(seen.read_text(encoding="utf-8"))["level"] == "log"

    def test_legacy_command_without_report_counts_as_delivered(
        self, fake_cfg, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg, _ = fake_cfg
        monkeypatch.setenv("FAKE_REPORT", "")
        assert send(cfg, "hi").channels == ("notify_cmd",)

    def test_falls_back_to_tree_script(self, tmp_path: Path) -> None:
        cfg = DaylogConfig(root=tmp_path)
        assert notify.notify_command(cfg) == []
        with pytest.raises(NotifyError, match="no notify_cmd"):
            send(cfg, "hi")
        script = tmp_path / notify.NOTIFY_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        assert notify.notify_command(cfg) == [sys.executable, str(script)]

    def test_load_config_reads_notify_cmd(self, tmp_path: Path) -> None:
        (tmp_path / ".smriti").mkdir()
        (tmp_path / ".smriti" / "daylog.json").write_text(
            json.dumps({"notify_cmd": ["py", "x.py"]}), encoding="utf-8")
        assert notify.notify_command(load_config(tmp_path)) == ["py", "x.py"]


def test_parse_channels() -> None:
    assert parse_channels("telegram: ok\ndelivered via: telegram, app-push\n") == (
        "telegram", "app-push")
    assert parse_channels("delivered via: NOTHING") == ()
    assert parse_channels("no report") is None

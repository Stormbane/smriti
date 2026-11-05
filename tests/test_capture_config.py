"""Capture is off until it is configured (H11.1).

With no daylog.json, or one without a "sources" key, the day-log used to
capture every Claude Code transcript and Codex rollout on the machine. Now
it captures nothing until daylog.json says "capture": true (the default
sources) or lists "sources", and logd and `smriti status` say why nothing
is captured and how to turn it on. Explicit lists work as they did.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from smriti.core.tree import tree_root
from smriti.daylog.config import capture_note, load_config

DEFAULTS = ["claude", "codex", "voice"]
MINE = {"name": "mine", "kind": "claude_jsonl", "glob": "/a/*.jsonl"}
PHONE = {"name": "phone", "kind": "voice_md", "glob": "/phone/*.md"}


def _daylog(root: Path, data: object) -> None:
    path = root / ".smriti" / "daylog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def test_without_a_daylog_json_nothing_is_captured(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.sources == []
    assert cfg.capture_off == "there is no .smriti/daylog.json"
    note = capture_note(cfg)
    assert note.startswith("off: there is no .smriti/daylog.json. To turn it on")
    assert "--capture" in note and '"capture": true' in note
    assert str(tmp_path / ".smriti" / "daylog.json") in note


@pytest.mark.parametrize("data,names,why", [
    # Never chosen: nothing (these captured every transcript before H11.1).
    ({}, [], 'neither says "capture": true nor lists "sources"'),
    ({"user_name": "Sam", "profile": "reference"}, [],
     'neither says "capture": true nor lists "sources"'),
    ('{"user_name": "Sam",', [], "isn't valid JSON"),
    ([1, 2], [], "isn't a JSON object"),
    ({"capture": "yes"}, [], '"capture" in .smriti/daylog.json is "yes", not true'),
    # Turned on: the defaults, and what is added to them.
    ({"capture": True}, DEFAULTS, ""),
    ({"capture": True, "extra_sources": [PHONE]}, [*DEFAULTS, "phone"], ""),
    # Explicit lists: exactly as before.
    ({"sources": []}, [], 'says "sources": []'),
    ({"capture": True, "sources": []}, [], 'says "sources": []'),
    ({"sources": [MINE]}, ["mine"], ""),
    ({"sources": [MINE], "extra_sources": [PHONE]}, ["mine", "phone"], ""),
    ({"sources": [], "extra_sources": [PHONE]}, ["phone"], ""),
    ({"extra_sources": [PHONE]}, ["phone"], ""),
    ({"sources": [{"name": 1}]}, [], 'none of the "sources"'),
    # Off, whatever else the file lists.
    ({"capture": False, "sources": [MINE], "extra_sources": [PHONE]}, [],
     'says "capture": false'),
])
def test_what_daylog_json_captures(tmp_path: Path, data: object, names: list[str],
                                   why: str) -> None:
    _daylog(tmp_path, data)
    cfg = load_config(tmp_path)
    assert [spec.name for spec in cfg.sources] == names
    assert (why in cfg.capture_off) if why else cfg.capture_off == ""
    assert capture_note(cfg).startswith("off: " if not names else "on: ")


def test_the_other_keys_still_load_when_capture_is_off(tmp_path: Path) -> None:
    _daylog(tmp_path, {"user_name": "Sam", "agent_name": "Ari", "profile": "x"})
    cfg = load_config(tmp_path)
    assert (cfg.sources, cfg.user_name, cfg.agent_name, cfg.profile) == ([], "Sam", "Ari", "x")


# ── What logd and `smriti status` say ─────────────────────────────────


def test_logd_says_capture_is_off_and_how_to_turn_it_on(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from smriti.daylog import logd

    seen: list[int] = []

    def collect(cfg: object) -> object:
        seen.append(len(cfg.sources))  # type: ignore[attr-defined]
        return type("Report", (), {"appended": 0})()

    monkeypatch.setattr(logd, "collect_once", collect)
    with caplog.at_level(logging.INFO, logger="smriti.daylog.logd"):
        logd.run_logd(interval_s=0, max_passes=1)
    assert seen == [0]  # nothing to read: no transcript is touched
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert message.startswith("logd: capture is off: there is no .smriti/daylog.json")
    assert "--capture" in message and '"capture": true' in message


def test_logd_says_when_daylog_json_turns_capture_on(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import os
    import time

    from smriti.daylog import logd

    seen: list[int] = []

    def collect(cfg: object) -> object:
        seen.append(len(cfg.sources))  # type: ignore[attr-defined]
        if len(seen) == 1:  # the installer says yes, while the daemon runs
            _daylog(tree_root(), {"capture": True})
            later = time.time() + 5
            os.utime(tree_root() / ".smriti" / "daylog.json", (later, later))
        return type("Report", (), {"appended": 0})()

    monkeypatch.setattr(logd, "collect_once", collect)
    with caplog.at_level(logging.INFO, logger="smriti.daylog.logd"):
        logd.run_logd(interval_s=0, max_passes=2)
    assert seen == [0, 3]
    assert any(r.getMessage().startswith("logd: daylog.json changed; watching 3 sources")
               for r in caplog.records)


def test_status_says_capture_is_off_even_without_an_index(
        capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Status:     Not indexed" in out
    assert "Capture:    off: there is no .smriti/daylog.json. To turn it on" in out
    assert "Day-log:" not in out  # nothing to scan, so nothing "not started" or "STALLED"


def test_status_says_which_sources_are_captured(capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    _daylog(tree_root(), {"capture": True, "extra_sources": [PHONE]})
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Capture:    on: claude, codex, voice, phone" in out
    assert "Day-log:    not started (run 'smriti logd')" in out

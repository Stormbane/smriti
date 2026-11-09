"""session_end: the UserPromptSubmit hook that asks for memory before goodbye."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from smriti_reference import session_end
from smriti_reference.session_end import REMINDER, reminder_for


@pytest.mark.parametrize("prompt", ["ok, bye for now", "Good night!", "let's call it here"])
def test_a_goodbye_gets_the_reminder(prompt: str) -> None:
    assert reminder_for({"prompt": prompt}) == REMINDER


@pytest.mark.parametrize("payload", [
    {"prompt": "fix the parser"}, {"prompt": ""}, {"prompt": 3}, {}, ["bye"], None,
])
def test_anything_else_gets_nothing(payload: object) -> None:
    assert reminder_for(payload) is None


def _run_main(monkeypatch: pytest.MonkeyPatch, data: bytes) -> int:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    return session_end.main()


def test_main_prints_the_reminder(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "thanks, signing off"}
    assert _run_main(monkeypatch, json.dumps(payload).encode()) == 0
    assert "--- SESSION ENDING ---" in capsys.readouterr().out


@pytest.mark.parametrize("data", [b"", b"{not json", b"\xff\xfe"])
def test_main_stays_out_of_the_way_of_a_bad_payload(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        data: bytes) -> None:
    assert _run_main(monkeypatch, data) == 0
    assert capsys.readouterr().out == ""


def test_it_runs_as_the_settings_hook_runs_it() -> None:
    result = subprocess.run(
        [sys.executable, "-P", "-m", "smriti_reference.session_end"],
        input=json.dumps({"prompt": "goodbye"}).encode(),
        capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0
    assert b"--- SESSION ENDING ---" in result.stdout

"""The hourly backup: the ``smriti backup`` command and its scheduled task."""
from __future__ import annotations

from pathlib import Path

import pytest

from smriti import cli
from smriti.daylog import schedule


def test_backup_command_triggers_with_the_tag_and_push(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    def fake_trigger(tag: str, *, push: bool = False, root: Path | None = None,
                     timeout: int = 900) -> None:
        calls.append((tag, push))

    monkeypatch.setattr("smriti.backup.trigger", fake_trigger)
    assert cli.main(["backup", "--push", "--tag", "hourly"]) == 0
    assert cli.main(["backup"]) == 0
    assert calls == [("hourly", True), ("manual", False)]


def test_a_backup_that_cannot_run_warns_and_does_not_raise(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """H9.9: trigger swallowed every failure without a trace."""
    from smriti import backup

    (tmp_path / ".smriti").mkdir()
    (tmp_path / ".smriti" / "backup.py").write_text("", encoding="utf-8")

    def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("no interpreter")

    monkeypatch.setattr(backup.subprocess, "Popen", cannot_start)
    with caplog.at_level("WARNING", logger="smriti.backup"):
        backup.trigger("nightly", root=tmp_path)
    assert "backup (nightly) did not run: no interpreter" in caplog.text


def test_the_backup_task_is_hourly_windowless_and_pushes() -> None:
    tasks = {name: (cmd, when, windowless) for name, cmd, when, windowless in schedule.TASKS}
    cmd, when, windowless = tasks["smriti-backup"]
    assert cmd == "backup --push --tag hourly"
    assert when[:2] == ["/sc", "hourly"]
    assert windowless

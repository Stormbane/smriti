"""A running logd follows daylog.json, so turning capture off takes effect at once."""

from __future__ import annotations

import json
import os
import time

import pytest

from smriti.core.tree import tree_root
from smriti.daylog import logd


def test_turning_capture_off_reaches_a_running_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review of H9.11: logd loaded its config once, so the installer's
    --no-capture changed the file but the daemon kept capturing."""
    config = tree_root() / ".smriti" / "daylog.json"
    seen: list[int] = []

    def collect(cfg: object) -> object:
        seen.append(len(cfg.sources))  # type: ignore[attr-defined]
        if len(seen) == 1:  # the installer says no, while the daemon runs
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({"sources": []}), encoding="utf-8")
            later = time.time() + 5
            os.utime(config, (later, later))
        return type("Report", (), {"appended": 0})()

    monkeypatch.setattr(logd, "collect_once", collect)
    logd.run_logd(interval_s=0, max_passes=2)
    assert seen[0] > 0 and seen[1] == 0


def test_a_config_passed_in_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.daylog.config import DaylogConfig

    config = tree_root() / ".smriti" / "daylog.json"
    given = DaylogConfig(root=tree_root(), sources=[])
    seen: list[object] = []

    def collect(cfg: object) -> object:
        seen.append(cfg)
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"location": f"x{len(seen)}"}), encoding="utf-8")
        return type("Report", (), {"appended": 0})()

    monkeypatch.setattr(logd, "collect_once", collect)
    logd.run_logd(given, interval_s=0, max_passes=2)
    assert seen == [given, given]

"""Point the user's home at a temporary directory for one test."""

from __future__ import annotations

from pathlib import Path

import pytest


def use_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Make ``Path.home()`` and ``~`` name *home*: HOME, and USERPROFILE (Windows reads that)."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    return home

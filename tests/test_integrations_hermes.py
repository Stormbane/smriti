"""End-to-end delivery tests for Hermes's materialized SOUL.md."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from smriti.integrations.hermes import install as hermes_install
from smriti.store import wake_summary


@pytest.fixture
def hermes_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    memory_root = tmp_path / ".narada"
    context = memory_root / ".smriti" / "context"
    context.mkdir(parents=True)
    (memory_root / ".smriti" / "wake-context.md").write_text(
        "# Core identity\nI am Narada.\n", encoding="utf-8"
    )
    (context / "hermes.md").write_text(
        "You are Narada's receptionist.\n", encoding="utf-8"
    )
    soul = tmp_path / ".hermes" / "SOUL.md"
    monkeypatch.setattr(hermes_install, "SOUL_MD", soul)
    return memory_root, soul


def test_install_delivers_core_and_hermes_overlay_and_replaces_hardlink(
    hermes_tree: tuple[Path, Path],
) -> None:
    memory_root, soul = hermes_tree
    soul.parent.mkdir(parents=True)
    os.link(memory_root / ".smriti" / "wake-context.md", soul)

    hermes_install.run(memory_root)

    delivered = soul.read_text(encoding="utf-8")
    assert "I am Narada." in delivered
    assert "You are Narada's receptionist." in delivered
    assert os.stat(soul).st_ino != os.stat(
        memory_root / ".smriti" / "wake-context.md"
    ).st_ino


def test_wake_rebuild_refreshes_live_hermes_delivery(
    hermes_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_root, soul = hermes_tree
    identity = memory_root / "identity.md"
    identity.write_text("# Identity\nChanged source identity.\n", encoding="utf-8")

    def executor(_prompt: str) -> tuple[str, object]:
        return "# Rebuilt core\nThe synchronized identity.\n", object()

    rebuilt = wake_summary.rebuild(memory_root, executor_fn=executor)

    assert rebuilt == memory_root / ".smriti" / "wake-context.md"
    delivered = soul.read_text(encoding="utf-8")
    assert "The synchronized identity." in delivered
    assert "You are Narada's receptionist." in delivered

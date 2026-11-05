"""Test isolation: no test may touch the real memory tree or its metrics.

Without this, any code path that falls back to ``tree_root()`` (the default
``~/.narada``) wrote into the live tree, and the process-wide metrics
logger kept appending pytest events to the live ``metrics.jsonl``: about
2,600 of them, which made those metrics useless as health signals.

HOME is deliberately left alone: the embedding model cache lives under it,
and a temp HOME would re-download the model for every run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("SMRITI_EMBED_PROVIDER", "").strip().lower() == "stub":
        skip = pytest.mark.skip(reason="requires semantic embeddings; stub hashes words only")
        for item in items:
            if item.get_closest_marker("needs_model") is not None:
                item.add_marker(skip)
    cli_items = [item for item in items if item.get_closest_marker("needs_cli") is not None]
    if cli_items:
        why = _claude_cli_unready()
        if why:
            for item in cli_items:
                item.add_marker(pytest.mark.skip(reason=why))


def _claude_cli_unready() -> str:
    """Why the real-CLI tests can't run here; "" when they can."""
    if os.environ.get("SMRITI_LIVE_CLI_TESTS") != "1":
        # Opt-in: they spend real money and a few seconds, and suites run often.
        return "live CLI tests are opt-in: set SMRITI_LIVE_CLI_TESTS=1"
    if os.environ.get("CI"):
        return "CI has no Claude Code login"
    cli = shutil.which("claude")
    if cli is None:
        return "the claude CLI is not installed"
    try:
        result = subprocess.run([cli, "auth", "status"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return "`claude auth status` gave no answer"
    if not (isinstance(status, dict) and status.get("loggedIn") is True):
        return "the claude CLI is not logged in"
    return ""


@pytest.fixture(autouse=True)
def _isolated_memory_root(tmp_path_factory: pytest.TempPathFactory,
                          monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    import smriti.metrics as metrics

    root = tmp_path_factory.mktemp("memory-root")
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    monkeypatch.setenv("NARADA_ROOT", str(root))
    monkeypatch.delenv("SMRITI_INTERNAL", raising=False)
    # The day boundary follows the machine's zone by default; tests mustn't
    # (CI runs in UTC). Tests of that default delete this.
    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
    # No test starts the embedding service unless it asks to (and then only
    # against temporary roots); its key, lock and discovery files go to a
    # temporary runtime directory, never the user's.
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "0")
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    metrics._instance = None  # re-resolve the metrics path under the temp root
    yield root
    metrics._instance = None

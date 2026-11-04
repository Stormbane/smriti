"""Test isolation: no test may touch the real memory tree or its metrics.

Without this, any code path that falls back to ``tree_root()`` (the default
``~/.narada``) wrote into the live tree, and the process-wide metrics
logger kept appending pytest events to the live ``metrics.jsonl``: about
2,600 of them, which made those metrics useless as health signals.

HOME is deliberately left alone: the embedding model cache lives under it,
and a temp HOME would re-download the model for every run.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_memory_root(tmp_path_factory: pytest.TempPathFactory,
                          monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    import smriti.metrics as metrics

    root = tmp_path_factory.mktemp("memory-root")
    monkeypatch.setenv("SMRITI_ROOT", str(root))
    monkeypatch.setenv("NARADA_ROOT", str(root))
    monkeypatch.delenv("SMRITI_INTERNAL", raising=False)
    metrics._instance = None  # re-resolve the metrics path under the temp root
    yield root
    metrics._instance = None

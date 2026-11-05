"""Test isolation for the reference profile: no test may touch the real memory tree.

The same isolation as smriti's own ``tests/conftest.py``: every test gets a
temporary memory root (SMRITI_ROOT and the legacy NARADA_ROOT), a fixed day
zone, no embedding service and a temporary runtime directory, and the
process-wide metrics logger is re-resolved under the temporary root.

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
    # The day boundary follows the machine's zone by default; tests mustn't
    # (CI runs in UTC).
    monkeypatch.setenv("SMRITI_DAY_UTC_OFFSET", "10")
    # No test starts the embedding service; its key, lock and discovery files
    # would go to a temporary runtime directory, never the user's.
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "0")
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    metrics._instance = None  # re-resolve the metrics path under the temp root
    yield root
    metrics._instance = None


@pytest.fixture()
def reference_profile(_isolated_memory_root: Path) -> Path:
    """The isolated memory root, configured like the reference installation."""
    from _reference import enable_reference

    enable_reference(_isolated_memory_root)
    return _isolated_memory_root

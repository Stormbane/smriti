"""The discovery file: a refused read is not "no service".

Windows refuses to open a file while ``os.replace`` swaps it, which is what
a service does when it publishes its ready record. ``read_discovery`` read
that refusal as no service, so a client could spawn a second service or,
with spawning off, fail a query (the flaky
``test_a_query_does_not_wait_while_the_service_is_starting``; a probe saw 80
refused reads in 18,733). It now reads again for up to 50 ms.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from smriti.core.tree import tree_root
from smriti.embed.runtime import (
    Discovery,
    read_discovery,
    runtime_dir,
    service_paths,
    write_discovery,
)


@pytest.fixture()
def paths() -> Any:
    found = service_paths(runtime_dir(), tree_root())
    write_discovery(found, Discovery(pid=4242, state="ready", instance="ab12", port=50001,
                                     root=str(tree_root())))
    return found


def _refuse(monkeypatch: pytest.MonkeyPatch, target: Path, times: int) -> list[float]:
    """Make reading ``target`` raise PermissionError ``times`` times; when each was refused."""
    real = Path.read_text
    refused: list[float] = []

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if self == target and len(refused) < times:
            refused.append(time.monotonic())
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return refused


def test_a_read_refused_while_the_record_is_replaced_is_tried_again(
        paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    refused = _refuse(monkeypatch, paths.discovery, times=1)
    found = read_discovery(paths)
    assert len(refused) == 1
    assert found is not None and (found.instance, found.state, found.port) == (
        "ab12", "ready", 50001)


def test_refusals_are_waited_out_for_about_50_ms_then_read_as_none(
        paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    refused = _refuse(monkeypatch, paths.discovery, times=4)
    assert read_discovery(paths) is not None  # four refusals: the fifth read succeeds
    refused = _refuse(monkeypatch, paths.discovery, times=100)
    t0 = time.monotonic()
    assert read_discovery(paths) is None
    assert len(refused) == 5 and 0.04 <= time.monotonic() - t0 < 1.0


def test_a_missing_file_is_no_service_at_once(paths: Any) -> None:
    paths.discovery.unlink()
    t0 = time.monotonic()
    assert read_discovery(paths) is None
    assert time.monotonic() - t0 < 0.04

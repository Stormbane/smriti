"""scripts/bench_capture.py: its synthetic transcripts are captured as the table says.

The benchmark itself spawns a process per side and round, too slow for the
suite; this runs one measurement in-process, small, against this checkout.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_capture.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_bench_capture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smriti_bench_capture"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules["smriti_bench_capture"]
    return module


@pytest.mark.parametrize("codex", ["legacy", "paginated"])
def test_a_measurement_captures_its_synthetic_turns(
        bench: ModuleType, codex: str, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(lines=600, codex=codex, seed=7)
    assert bench.child(args) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    first, idle, more = result["appended"]
    # The turns the adapters keep: half the Claude lines, a third of the Codex ones,
    # in either Codex encoding.
    assert first == 600 // 2 + 600 // 3 and idle == 0 and more > 0
    assert result["first_s"] > 0 and result["idle_s"] > 0

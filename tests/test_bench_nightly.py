"""scripts/bench_nightly.py: its measurements run, and its schedule check reports a plan.

The benchmark spawns a process per side and round on copies of a real tree,
too slow for the suite; this runs each kind of measurement in-process on a
small synthetic tree (the user Sam, the agent Ari), against this checkout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from smriti.daylog import collect, nightly
from smriti.daylog import digest as digest_mod
from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import AGENT, USER, Turn, parse_ts
from smriti.daylog.writer import append_turns

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_nightly.py"
DAYS = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-09")


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_bench_nightly", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smriti_bench_nightly"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules["smriti_bench_nightly"]
    return module


def _tree(root: Path) -> None:
    cfg = DaylogConfig(root=root, sources=[])
    for day in DAYS:
        ts = parse_ts(f"{day}T01:00:00Z")
        assert ts is not None
        append_turns(cfg, [Turn(ts=ts, channel="voice", who=USER, text=f"Sam on {day}",
                                session="s1"),
                           Turn(ts=ts, channel="voice", who=AGENT, text=f"Ari on {day}",
                                session="s1")])


def test_a_timing_round_measures_every_step(bench: ModuleType, tmp_path: Path) -> None:
    _tree(tmp_path)
    out = bench.child_time(tmp_path)
    for key, _name in bench.MEASURES:
        assert out[key] is not None and out[key] >= 0, key
    assert out["stale"] == len(DAYS) and out["appended"] == 0
    assert out["rendered"] == ["2026-09-02", "2026-09-03", "2026-09-09", "2026-09-01"]
    assert out["nightly_ok"] is False  # the synthetic backup log ends in failed pushes


def test_the_schedule_check_reports_each_night(
        bench: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The check stubs these in its own process; here, restore them afterwards.
    monkeypatch.setattr(digest_mod, "get_provider", digest_mod.get_provider)
    monkeypatch.setattr(collect, "collect_once", collect.collect_once)
    monkeypatch.setattr(nightly, "build_rollup", nightly.build_rollup)
    monkeypatch.setattr(nightly, "enumerate_stale_periods", nightly.enumerate_stale_periods)
    _tree(tmp_path)
    plan = bench.child_schedule(tmp_path, nights=2)["nights"]
    first, second = plan
    assert first["digested"] == sorted(DAYS, reverse=True)
    assert first["model_calls"] == len(DAYS) + len(first["rolled_up"])
    assert first["rolled_up"] == first["rollups_due"][:2]  # the default cap
    assert second["digested"] == []

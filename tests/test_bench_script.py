"""scripts/bench.py: the parts that decide what the benchmark reports.

The benchmark itself runs real servers on copies of a tree, which is far
too slow for the suite; these cover the logic around it: the round order,
each side's environment, the queries, the parity check and the verdicts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smriti_bench", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smriti_bench"] = module  # dataclasses look their module up
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules["smriti_bench"]
    return module


def test_rounds_alternate_so_drift_falls_on_both_sides(bench: ModuleType) -> None:
    assert list(bench.order(1)) == [0, 1]
    assert list(bench.order(2)) == [0, 1, 1, 0]
    assert list(bench.order(3)) == [0, 1, 1, 0, 0, 1]


def test_each_side_runs_its_own_code_on_its_own_copy(bench: ModuleType, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    base = {"SMRITI_ROOT": "/the/live/tree", "NARADA_ROOT": "/the/live/tree",
            "SMRITI_EMBED_SERVICE": "0", "PYTHONPATH": "/elsewhere", "HOME": "/home/sam"}

    env = bench.side_env(base, checkout, tmp_path / "copy", tmp_path / "run")

    assert env["PYTHONPATH"] == str(checkout / "src")
    assert env["SMRITI_ROOT"] == env["NARADA_ROOT"] == str(tmp_path / "copy")
    assert env["SMRITI_RUNTIME_DIR"] == str(tmp_path / "run")
    assert "SMRITI_EMBED_SERVICE" not in env and env["HOME"] == "/home/sam"
    assert "HF_HUB_OFFLINE" not in env  # the local provider can't load a model offline

    (checkout / "profiles" / "reference" / "src").mkdir(parents=True)
    env = bench.side_env(base, checkout, tmp_path / "copy", tmp_path / "run", SMRITI_WAKE="1")
    assert env["PYTHONPATH"].split(bench.os.pathsep) == [
        str(checkout / "src"), str(checkout / "profiles" / "reference" / "src")]
    assert env["SMRITI_WAKE"] == "1"


def test_queries_come_from_a_file_or_a_generic_list(bench: ModuleType, tmp_path: Path) -> None:
    assert bench.load_queries(None) == list(bench.DEFAULT_QUERIES)
    assert not any(name in q for q in bench.DEFAULT_QUERIES for name in ("Suti", "Narada"))
    path = tmp_path / "queries.txt"
    path.write_text("# what Ari asks\n\nbrass lantern\n  marigolds by the well  \n",
                    encoding="utf-8")
    assert bench.load_queries(path) == ["brass lantern", "marigolds by the well"]
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bench.load_queries(path)


def test_top5_reads_the_sources_and_marks_a_keyword_only_answer(bench: ModuleType) -> None:
    text = ("keyword-only: the embedding service is starting\n\n"
            "[1] notes/lantern.md :: Lantern (score: 0.91, depth: 1)\nAri hung it.\n\n"
            "[2] people/sam.md (score: 0.40, depth: 1)\nSam.\n")
    assert bench.top5(text) == ["<keyword-only>", "notes/lantern.md", "people/sam.md"]


def test_parity_compares_lists_not_sets(bench: ModuleType) -> None:
    a = {"q1": ["x.md", "y.md"], "q2": ["x.md", "y.md"], "q3": ["x.md", "x.md", "y.md"]}
    b = {"q1": ["x.md", "y.md"], "q2": ["y.md", "x.md"], "q3": ["x.md", "y.md", "y.md"]}
    assert bench.parity(a, b) == [
        ("q1", "same"), ("q2", "same results, another order"),
        ("q3", "differs: 2/3 shared"),  # the same set, but not the same results
    ]


def _side(bench: ModuleType, name: str, rounds: list[dict[str, Any]]) -> Any:
    side = bench.Side(name, f"label-{name}", Path("."), Path("."), Path("."))
    side.rounds.extend(rounds)
    return side


def test_a_measure_more_than_ten_percent_slower_is_named(bench: ModuleType) -> None:
    a = _side(bench, "A", [{"side": "A", "round": 1, "read_warm_p50_ms": 100.0,
                            "write_p50_ms": 50.0, "wake_ms": 700.0, "mcp_rss_mb": 40.0,
                            "top5": {"q": ["x.md"]}}])
    b = _side(bench, "B", [{"side": "B", "round": 1, "read_warm_p50_ms": 109.0,
                            "write_p50_ms": 60.0, "wake_ms": 500.0, "mcp_rss_mb": 900.0,
                            "top5": {"q": ["x.md"]}, "problems": ["smriti index exited 1"]}])

    table, late = bench.report(a, b)

    assert late == ["write_p50_ms"]  # 1.20; 1.09 is within the margin; memory isn't time
    assert "| smriti_write: p50 | 50 ms | 60 ms | 1.20 (slower) |" in table
    assert "| smriti_read: warm p50 | 100 ms | 109 ms | 1.09 |" in table
    assert "| wake hook (median of 3) | 700 ms | 500 ms | 0.71 |" in table
    assert "| embedding service memory | - | - | - |" in table
    assert "- q: same" in table
    assert "B1: smriti index exited 1" in table


def test_means_skip_rounds_without_the_measure(bench: ModuleType) -> None:
    rounds = [{"wake_ms": 600.0}, {"wake_ms": 800.0}, {}, {"wake_ms": True}]
    assert bench.mean_of(rounds, "wake_ms") == 700.0
    assert bench.mean_of(rounds, "status_ms") is None
    assert bench.cell(1530.0, "ms") == "1.53 s" and bench.cell(81.4, "ms") == "81 ms"


def test_a_source_with_spaces_is_read_whole(bench: ModuleType) -> None:
    # Two different files must not look like the same result (Codex review of H12.0).
    text = ("[1] notes/storage design.md :: Why (score: 0.91, depth: 2)\nbody\n\n"
            "[2] notes/storage migration.md (score: 0.50, depth: 1)\n")
    assert [s for _, s in bench.RESULT_RE.findall(text)] == [
        "notes/storage design.md", "notes/storage migration.md"]


class _Answers:
    """Stands in for Mcp.call, answering each tools/call with a canned response."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def __call__(self, method: str, params: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        return 1.0, self.data


@pytest.mark.parametrize("data", [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Internal error"}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Error: no"}],
                                           "isError": True}},
    {"jsonrpc": "2.0", "id": 1, "result": {}},
])
def test_a_failed_tool_call_is_never_timed_as_a_result(bench: ModuleType,
                                                       data: dict[str, Any]) -> None:
    mcp = object.__new__(bench.Mcp)
    mcp.call = _Answers(data)  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        mcp.tool("smriti_read", {"query": "q"})


def test_a_tree_written_through_a_link_is_refused(bench: ModuleType, tmp_path: Path) -> None:
    # A link kept by the copy would send the bench's writes into another tree.
    import os

    outside, copy = tmp_path / "outside", tmp_path / "copy"
    (outside / ".smriti").mkdir(parents=True)
    copy.mkdir()
    try:
        os.symlink(outside / ".smriti", copy / ".smriti", target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("this account can't create symlinks")
        import _winapi  # a junction needs no privilege, and the copies hold junctions

        _winapi.CreateJunction(str(outside / ".smriti"), str(copy / ".smriti"))
    with pytest.raises(SystemExit, match="written through links"):
        bench.check_writable(copy)
    if sys.platform == "win32":
        os.rmdir(copy / ".smriti")  # removes the junction, not its target
    else:
        os.unlink(copy / ".smriti")
    assert (outside / ".smriti").is_dir()  # removing the link left its target alone
    (copy / ".smriti").mkdir()
    bench.check_writable(copy)  # a plain directory is fine

"""Tier-1 performance regression guards.

These tests don't ship correctness assertions — the unit tests do that.
Their job is to catch order-of-magnitude regressions in hot paths
during local PR review or release branches. Bounds are deliberately
loose (typically 5-10x the observed median) so they don't false-fire
on a slow CI box; they're aimed at catching 10x slowdowns that
indicate something architecturally bad happened.

All tests are marked ``@pytest.mark.slow`` and excluded from the
default ``pytest`` run via ``addopts`` in pyproject.toml. Run them
explicitly:

    pytest -m slow -v

Median timings on a 2026-era Windows dev box (Python 3.12, NVMe):
    briefing()                    ~30 ms
    compose_agent_doc()           ~3 ms
    _extract_paths (1000 lines)   ~1 ms
    wrap_tool overhead            ~0.5 ms
    deploy_hook_scripts (100)     ~50 ms

Bounds chosen at ~10x median to leave headroom for slow boxes while
still flagging real architectural regressions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


# --- Helpers -------------------------------------------------------------

class _Timer:
    """Context manager that captures elapsed seconds."""

    def __enter__(self) -> "_Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed_s = time.perf_counter() - self._t0

    @property
    def ms(self) -> float:
        return self.elapsed_s * 1000


def _populate_minimal_memory_tree(root: Path) -> None:
    """Drop a small but realistic memory tree under ``root``.

    Mirrors the structure briefing() walks: identity, journal, wake-context.
    """
    (root / ".smriti").mkdir(parents=True, exist_ok=True)
    (root / ".smriti" / "wake-context.md").write_text(
        "# Identity\n\nPerf test entity.\n" + ("filler line\n" * 50),
        encoding="utf-8",
    )
    journal = root / "journal" / "2026" / "05" / "week1"
    journal.mkdir(parents=True)
    for d in range(1, 6):
        (journal / f"05-{d:02d}.md").write_text(
            f"# {d}\nentry content\n" + ("filler line\n" * 30),
            encoding="utf-8",
        )
    (root / "open-threads").mkdir(parents=True, exist_ok=True)
    (root / "open-threads" / "open-threads.md").write_text(
        "# Open threads\n\n- one\n- two\n", encoding="utf-8",
    )


# --- briefing() ----------------------------------------------------------

class TestBriefingPerf:
    def test_briefing_under_300ms(self, tmp_path):
        """briefing() reads many small files — should stay under
        300ms even on a slow box. Logs the actual time."""
        from smriti.wake import briefing

        _populate_minimal_memory_tree(tmp_path)

        # Warm the FS cache so we measure assembly cost, not first-touch I/O.
        briefing(memory_root=tmp_path, cwd=tmp_path)

        with _Timer() as t:
            out = briefing(memory_root=tmp_path, cwd=tmp_path)

        print(f"\n  briefing(): {t.ms:.1f}ms ({len(out)} chars)")
        assert t.ms < 300, f"briefing() took {t.ms:.1f}ms; bound is 300ms"
        # Sanity: it actually produced something.
        assert len(out) > 100


# --- compose_agent_doc() -------------------------------------------------

class TestComposeAgentDocPerf:
    def test_compose_under_50ms(self):
        from smriti.integrations.common import compose_agent_doc

        addendum = "## Harness\n" + ("filler line\n" * 20)

        # Warm.
        compose_agent_doc(addendum=addendum, memory_rel="~/.narada", header="# H")

        with _Timer() as t:
            out = compose_agent_doc(
                addendum=addendum, memory_rel="~/.narada", header="# H",
            )

        print(f"\n  compose_agent_doc(): {t.ms:.1f}ms ({len(out)} chars)")
        assert t.ms < 50, f"compose_agent_doc took {t.ms:.1f}ms; bound is 50ms"


# --- _extract_paths large patch -----------------------------------------

class TestExtractPathsPerf:
    def test_extract_1000_line_patch_under_50ms(self):
        from smriti.recall.hook import _extract_paths

        # Synthetic 1000-line patch with 50 file directives mixed in.
        lines = ["*** Begin Patch"]
        for i in range(50):
            lines.append(f"*** Update File: src/path/to/module_{i:04d}.py")
            for j in range(18):  # filler diff lines per file
                lines.append(f"  context line {j}")
            lines.append(f"-old line {i}")
            lines.append(f"+new line {i}")
        lines.append("*** End Patch")
        body = "\n".join(lines)

        # Warm.
        _extract_paths("apply_patch", {"command": body})

        with _Timer() as t:
            paths = _extract_paths("apply_patch", {"command": body})

        print(f"\n  _extract_paths (1000-line, 50 files): {t.ms:.1f}ms "
              f"({len(paths)} paths)")
        assert t.ms < 50, f"_extract_paths took {t.ms:.1f}ms; bound is 50ms"
        assert len(paths) == 50


# --- wrap_tool overhead --------------------------------------------------

class TestWrapToolOverheadPerf:
    def test_wrap_overhead_under_50ms(self, monkeypatch):
        """Overhead of the wrap_tool machinery itself, with run_recall
        stubbed to a no-op so we measure wrapping, not recall."""
        from smriti.recall import wrap_tool
        from smriti.recall import wrap as wrap_module
        from smriti.recall.types import RecallResponse

        def fake_recall(query, *args, **kwargs):
            return RecallResponse(matches=[], elapsed_ms=0, backend="stub")

        monkeypatch.setattr(wrap_module, "run_recall", fake_recall)

        def underlying(x: str) -> str:
            return x.upper()

        wrapped = wrap_tool(underlying)

        # Warm both paths.
        underlying("seed")
        wrapped("seed")

        # Run a small batch and average — single-call timings are noisy
        # at the sub-millisecond level.
        N = 100
        with _Timer() as t_raw:
            for _ in range(N):
                underlying("foo")
        with _Timer() as t_wrapped:
            for _ in range(N):
                wrapped("foo")

        overhead_ms = (t_wrapped.ms - t_raw.ms) / N
        print(f"\n  wrap_tool overhead: {overhead_ms:.3f}ms/call "
              f"(raw {t_raw.ms / N:.3f}, wrapped {t_wrapped.ms / N:.3f})")
        assert overhead_ms < 50, (
            f"wrap_tool added {overhead_ms:.3f}ms/call; bound is 50ms"
        )


# --- deploy_hook_scripts at scale ---------------------------------------

class TestDeployHookScriptsPerf:
    def test_deploy_100_scripts_under_2s(self, tmp_path):
        from smriti.integrations.common import deploy_hook_scripts

        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        names = []
        for i in range(100):
            name = f"hook_{i:03d}.py"
            (src / name).write_text(
                "# stub\nimport sys\nsys.exit(0)\n", encoding="utf-8",
            )
            names.append(name)

        # Warm filesystem.
        deploy_hook_scripts(src, dst, names[:1])

        with _Timer() as t:
            deploy_hook_scripts(src, dst, names)

        print(f"\n  deploy_hook_scripts (100 scripts): {t.ms:.1f}ms")
        assert t.ms < 2000, (
            f"deploy_hook_scripts took {t.ms:.1f}ms for 100 scripts; "
            "bound is 2000ms"
        )


# --- wake_runner JSON framing throughput --------------------------------

class TestWakeRunnerFramingPerf:
    """Catch a regression where codex-json framing introduces O(n²)
    string ops or similar. The framing wraps a ~10K char briefing into
    JSON — should be sub-millisecond."""

    def test_codex_json_framing_under_20ms(self):
        from smriti.integrations.common.wake_runner import _frame

        payload = "x" * 10_000  # representative briefing size

        # Warm.
        _frame(payload, "codex-json")

        N = 1000
        with _Timer() as t:
            for _ in range(N):
                _frame(payload, "codex-json")

        per_call_ms = t.ms / N
        print(f"\n  _frame(codex-json, 10KB): {per_call_ms:.3f}ms/call "
              f"({N} runs in {t.ms:.1f}ms)")
        assert per_call_ms < 20, (
            f"codex-json framing took {per_call_ms:.3f}ms/call; bound is 20ms"
        )

        # Bonus correctness check: output round-trips through json.loads.
        out = _frame(payload, "codex-json")
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["additionalContext"] == payload

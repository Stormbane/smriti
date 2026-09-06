"""Tests for the PostToolUse recall hook.

These exercise the hook's plumbing without requiring Claude Code or
Codex to be installed: we feed synthetic stdin payloads and assert on
stdout. ``run_recall`` is monkey-patched to return canned matches so
the tests don't depend on the qmd daemon or a populated index.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from smriti.recall import hook
from smriti.recall.types import RecallMatch, RecallResponse


@pytest.fixture(autouse=True)
def _isolated_tree(monkeypatch, tmp_path):
    """Point the memory tree at an empty temp root.

    The hook now also emits cross-channel presence read from the live
    day-log; without isolation these tests inherit whatever the
    machine's logd captured in the last 15 minutes (flaked live,
    2026-09-06)."""
    monkeypatch.setenv("SMRITI_ROOT", str(tmp_path / "tree"))


@pytest.fixture
def fake_recall(monkeypatch):
    """Replace run_recall with a stub returning canned matches."""

    def _factory(matches: list[RecallMatch] | None = None):
        recorded: list[str] = []

        def stub(query, cfg=None, log_extra=None):  # noqa: ARG001
            recorded.append(query)
            return RecallResponse(
                matches=matches or [],
                elapsed_ms=12,
                backend="stub",
            )

        monkeypatch.setattr(hook, "run_recall", stub)
        return recorded

    return _factory


def _run_main(payload: dict, monkeypatch) -> str:
    """Drive hook.main with payload on stdin; return captured stdout."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = hook.main()
    assert rc == 0, "hook must always exit 0; never break parent tool call"
    return buf.getvalue()


def test_skips_unknown_tool(fake_recall, monkeypatch):
    """Unknown tool names produce no output and never call run_recall."""
    recorded = fake_recall()
    out = _run_main(
        {"tool_name": "WeirdTool", "tool_input": {"file_path": "foo.py"}},
        monkeypatch,
    )
    assert out == ""
    assert recorded == [], "should not have queried recall"


def test_skips_bash(fake_recall, monkeypatch):
    """Bash is intentionally skipped (too noisy for ambient recall)."""
    recorded = fake_recall()
    out = _run_main(
        {"tool_name": "Bash", "tool_input": {"command": "cat src/foo.py"}},
        monkeypatch,
    )
    assert out == ""
    assert recorded == []


def test_skips_mcp_tool(fake_recall, monkeypatch):
    """MCP tools are skipped to avoid recall recursing on smriti_read."""
    recorded = fake_recall()
    out = _run_main(
        {"tool_name": "mcp__smriti__smriti_read", "tool_input": {"query": "x"}},
        monkeypatch,
    )
    assert out == ""
    assert recorded == []


def test_handles_claude_read(fake_recall, monkeypatch, tmp_path):
    """Claude Code's Read tool with a real file produces a recall block."""
    target = tmp_path / "decisions.md"
    target.write_text("# decisions\n\nWe decided X.\n", encoding="utf-8")

    recorded = fake_recall([
        RecallMatch(source="memory/decisions.md", snippet="prior X note", score=0.91),
    ])
    out = _run_main(
        {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
        monkeypatch,
    )
    assert "<system-reminder>" in out
    assert "memory/decisions.md" in out
    assert "score 0.91" in out
    assert recorded, "run_recall should have been called"
    assert "decisions" in recorded[0], "query should include the stem"


def test_skips_paths_in_memory_tree(fake_recall, monkeypatch, tmp_path):
    """Paths inside the memory tree are skipped to prevent recursion."""
    fake_home = tmp_path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    narada = fake_home / ".narada"
    (narada / "journal").mkdir(parents=True)
    target = narada / "journal" / "today.md"
    target.write_text("entry", encoding="utf-8")

    recorded = fake_recall([
        RecallMatch(source="x.md", snippet="x", score=0.9),
    ])
    out = _run_main(
        {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
        monkeypatch,
    )
    assert out == ""
    assert recorded == []


def test_handles_apply_patch_single_file(fake_recall, monkeypatch):
    """Codex apply_patch with one Update File: directive fires recall."""
    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: src/recall/hook.py\n"
        "@@\n"
        "-old\n+new\n"
        "*** End Patch\n"
    )
    recorded = fake_recall([
        RecallMatch(source="recall-design.md", snippet="design notes", score=0.85),
    ])
    out = _run_main(
        {"tool_name": "apply_patch", "tool_input": {"command": patch_body}},
        monkeypatch,
    )
    assert "<system-reminder>" in out
    assert "recall-design.md" in out
    assert recorded, "should have queried recall for the patched file"
    assert "hook" in recorded[0]


def test_handles_apply_patch_multiple_files(fake_recall, monkeypatch):
    """apply_patch with Add/Update/Delete fires recall on all named files."""
    patch_body = (
        "*** Begin Patch\n"
        "*** Add File: src/new_module.py\n"
        "+contents\n"
        "*** Update File: docs/USAGE.md\n"
        "@@\n"
        "-old\n+new\n"
        "*** Delete File: tests/old_test.py\n"
        "*** End Patch\n"
    )
    recorded = fake_recall([
        RecallMatch(source="usage-prior.md", snippet="prior usage doc", score=0.77),
    ])
    out = _run_main(
        {"tool_name": "apply_patch", "tool_input": {"command": patch_body}},
        monkeypatch,
    )
    assert "<system-reminder>" in out
    assert "3 files" in out, "header should say 3 files for multi-file patch"
    # Each path should have produced its own recall query.
    assert len(recorded) == 3
    assert any("new module" in q for q in recorded)
    assert any("USAGE" in q for q in recorded)
    assert any("old test" in q for q in recorded)


def test_apply_patch_dedupes_by_source(fake_recall, monkeypatch):
    """When multiple files surface the same memory match, dedupe by source."""
    patch_body = (
        "*** Begin Patch\n"
        "*** Update File: src/foo.py\n"
        "*** Update File: src/bar.py\n"
        "*** End Patch\n"
    )
    # Same source returned for both queries; should appear once.
    fake_recall([
        RecallMatch(source="design/shared.md", snippet="shared", score=0.80),
    ])
    out = _run_main(
        {"tool_name": "apply_patch", "tool_input": {"command": patch_body}},
        monkeypatch,
    )
    assert out.count("design/shared.md") == 1, "should dedupe by source"


def test_codex_json_framing(fake_recall, monkeypatch, tmp_path):
    """SMRITI_RECALL_FRAMING=codex-json wraps output as Codex hook JSON."""
    target = tmp_path / "thing.py"
    target.write_text("x = 1\n", encoding="utf-8")

    fake_recall([
        RecallMatch(source="m.md", snippet="snippet", score=0.92),
    ])
    monkeypatch.setenv("SMRITI_RECALL_FRAMING", "codex-json")
    out = _run_main(
        {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
        monkeypatch,
    )
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    # System-reminder tags stripped in codex-json framing — Codex frames
    # the text itself as developer context.
    assert "<system-reminder>" not in ctx
    assert "</system-reminder>" not in ctx
    assert "m.md" in ctx
    assert "score 0.92" in ctx


def test_no_matches_no_output(fake_recall, monkeypatch, tmp_path):
    """When recall returns zero matches, hook stays silent."""
    target = tmp_path / "thing.md"
    target.write_text("hi", encoding="utf-8")

    fake_recall([])  # empty matches
    out = _run_main(
        {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
        monkeypatch,
    )
    assert out == ""


def test_invalid_json_stdin_silent(monkeypatch):
    """Garbage stdin -> exit cleanly with no output."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = hook.main()
    assert rc == 0
    assert buf.getvalue() == ""


def test_empty_stdin_silent(monkeypatch):
    """Empty stdin -> silent."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = hook.main()
    assert rc == 0
    assert buf.getvalue() == ""


# --- Direct unit tests on the parsing layer ------------------------------

def test_extract_paths_apply_patch_directives():
    body = (
        "*** Begin Patch\n"
        "*** Add File: a.py\n"
        "*** Update File: nested/dir/b.md\n"
        "*** Delete File: c.toml\n"
        "*** End Patch\n"
    )
    paths = hook._extract_paths("apply_patch", {"command": body})
    assert paths == ["a.py", "nested/dir/b.md", "c.toml"]


def test_extract_paths_apply_patch_non_string_command():
    """Defensive: tool_input.command is sometimes not a string."""
    paths = hook._extract_paths("apply_patch", {"command": None})
    assert paths == []
    paths = hook._extract_paths("apply_patch", {"command": ["a", "b"]})
    assert paths == []


def test_extract_paths_claude_read():
    paths = hook._extract_paths("Read", {"file_path": "/abs/path.py"})
    assert paths == ["/abs/path.py"]


def test_extract_paths_claude_read_missing_field():
    paths = hook._extract_paths("Read", {})
    assert paths == []


def test_extract_paths_skipped_tools():
    assert hook._extract_paths("Bash", {"command": "cat foo"}) == []
    assert hook._extract_paths("mcp__smriti__smriti_read", {"q": "x"}) == []
    assert hook._extract_paths("WhateverElse", {"file_path": "x.py"}) == []


def test_presence_only_output_without_matches(fake_recall, monkeypatch, tmp_path):
    """Fresh other-channel activity is injected even with zero recall matches."""
    from datetime import datetime, timezone

    from smriti.daylog.config import load_config
    from smriti.daylog.model import Turn
    from smriti.daylog.writer import append_turns

    cfg = load_config()  # resolves to the isolated SMRITI_ROOT tree
    append_turns(cfg, [Turn(
        ts=datetime.now(timezone.utc), channel="telegram", who="suti",
        text="are you around?", session="t1",
    )])
    target = tmp_path / "thing.md"
    target.write_text("hi", encoding="utf-8")
    fake_recall([])
    out = _run_main(
        {"tool_name": "Read", "tool_input": {"file_path": str(target)},
         "cwd": str(tmp_path)},
        monkeypatch,
    )
    assert "Active on other channels" in out and "telegram" in out
    assert "<system-reminder>" in out

"""Tests for src/smriti/recall/wrap.py.

`wrap_tool` is the public helper non-Claude-Code harnesses use to
fire ambient recall after their own tool calls. It must be tolerant
of recall failures (recall is ambient, not load-bearing) and pass
the underlying tool's return value through unchanged.
"""

from __future__ import annotations

import pytest

from smriti.recall import ToolResult, wrap_tool
from smriti.recall import wrap as wrap_module
from smriti.recall.types import RecallMatch, RecallResponse


@pytest.fixture
def fake_recall(monkeypatch):
    """Replace run_recall in the wrap module with a stub."""

    def _factory(matches=None, raises=None):
        recorded = []

        def stub(query, *args, **kwargs):
            recorded.append(query)
            if raises:
                raise raises
            return RecallResponse(
                matches=matches or [],
                elapsed_ms=5,
                backend="stub",
            )

        monkeypatch.setattr(wrap_module, "run_recall", stub)
        return recorded

    return _factory


# --- Happy path ---------------------------------------------------------

class TestWrapToolHappyPath:
    def test_returns_tool_result_with_output(self, fake_recall):
        fake_recall([RecallMatch(source="m.md", snippet="x", score=0.9)])

        def my_tool(path):
            return f"contents of {path}"

        wrapped = wrap_tool(my_tool)
        result = wrapped("foo.txt")

        assert isinstance(result, ToolResult)
        assert result.output == "contents of foo.txt"
        assert result.recall is not None
        assert result.recall_block != ""

    def test_recall_block_includes_match(self, fake_recall):
        fake_recall([RecallMatch(source="prior.md", snippet="prior context", score=0.85)])

        wrapped = wrap_tool(lambda p: "out", name="my_read")
        result = wrapped("test.md")
        assert "<system-reminder>" in result.recall_block
        assert "tool: my_read" in result.recall_block
        assert "prior.md" in result.recall_block
        assert "score 0.85" in result.recall_block

    def test_default_query_uses_first_arg(self, fake_recall):
        recorded = fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        wrapped = wrap_tool(lambda p: p)
        wrapped("magic-query-arg")
        assert recorded == ["magic-query-arg"]

    def test_custom_query_from(self, fake_recall):
        recorded = fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        def reader(path, encoding="utf-8"):
            return f"<{path}>"

        # Derive query from kwargs + output, not just args[0].
        wrapped = wrap_tool(
            reader,
            query_from=lambda args, kw, out: f"{args[0]}|{kw.get('encoding')}",
        )
        wrapped("p.md", encoding="latin-1")
        assert recorded == ["p.md|latin-1"]


# --- Skip recall when no query --------------------------------------------

class TestWrapToolNoQuery:
    def test_skips_recall_when_query_from_returns_none(self, fake_recall):
        recorded = fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        wrapped = wrap_tool(
            lambda p: p,
            query_from=lambda a, kw, out: None,
        )
        result = wrapped("anything")
        assert result.recall is None
        assert result.recall_block == ""
        assert recorded == [], "run_recall should not have been called"

    def test_skips_when_no_args_and_default_query(self, fake_recall):
        recorded = fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        wrapped = wrap_tool(lambda: "no args ok")
        result = wrapped()
        assert result.output == "no args ok"
        assert result.recall is None
        assert recorded == []


# --- Recall is ambient: failures must NOT break the tool -------------------

class TestWrapToolFailureSwallowing:
    def test_swallows_recall_exception(self, fake_recall):
        fake_recall(raises=RuntimeError("daemon dead"))

        wrapped = wrap_tool(lambda p: f"<{p}>")
        result = wrapped("x.md")
        # Tool output preserved.
        assert result.output == "<x.md>"
        # Recall fields fall back to None / empty.
        assert result.recall is None
        assert result.recall_block == ""

    def test_swallows_query_from_exception(self, fake_recall):
        recorded = fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        def boom(args, kwargs, output):
            raise ValueError("can't compute query")

        wrapped = wrap_tool(lambda p: f"<{p}>", query_from=boom)
        result = wrapped("x.md")
        assert result.output == "<x.md>"
        assert recorded == []

    def test_underlying_tool_exceptions_NOT_swallowed(self, fake_recall):
        """Tool errors must bubble — only recall errors are swallowed."""
        fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        def broken(p):
            raise FileNotFoundError(p)

        wrapped = wrap_tool(broken)
        with pytest.raises(FileNotFoundError):
            wrapped("missing.md")


# --- Empty matches → empty recall_block -----------------------------------

class TestWrapToolEmptyMatches:
    def test_no_matches_means_empty_block(self, fake_recall):
        fake_recall([])  # zero matches

        wrapped = wrap_tool(lambda p: p)
        result = wrapped("foo.md")
        assert result.recall is not None  # recall ran
        assert len(result.recall.matches) == 0
        assert result.recall_block == ""


# --- Wrapper metadata -------------------------------------------------------

class TestWrapToolMetadata:
    def test_wrapped_function_name_prefixed(self):
        def my_function(x):
            return x

        wrapped = wrap_tool(my_function)
        assert wrapped.__name__ == "recall_wrapped_my_function"

    def test_explicit_name_overrides(self, fake_recall):
        fake_recall([RecallMatch(source="x", snippet="x", score=0.5)])

        wrapped = wrap_tool(lambda p: p, name="custom_label")
        result = wrapped("test")
        assert "tool: custom_label" in result.recall_block
        assert wrapped.__name__ == "recall_wrapped_custom_label"

    def test_doc_includes_underlying_doc(self):
        def my_tool(x):
            """Underlying tool that does something."""
            return x

        wrapped = wrap_tool(my_tool)
        assert wrapped.__doc__ is not None
        assert "Underlying tool that does something" in wrapped.__doc__

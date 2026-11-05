"""Tool wrapping — fire ambient recall after any callable.

Mirrors the Claude Code PostToolUse hook pattern in callable form so
non-Claude harnesses can decorate their own tools with the same
behavior. After the wrapped tool runs, recall fires against a query
derived from the tool's args/output and the result is bundled with a
``<system-reminder>``-shaped block that the harness can inject back
into the model's context.

Typical use::

    from smriti.recall import wrap_tool

    def read_file(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    wrapped = wrap_tool(read_file, query_from=lambda a, kw, out: a[0])

    result = wrapped("notes.md")
    print(result.output)         # the file content
    print(result.recall_block)   # injectable system-reminder text

The wrapper never raises on recall failures — recall is *ambient*,
not load-bearing for the tool call. It logs them as warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from smriti.recall.runner import run_recall
from smriti.recall.types import RecallResponse

log = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Wrapped-tool return value: original output + recall annotations."""

    output: Any
    recall: RecallResponse | None = None
    recall_block: str = ""


QueryDeriver = Callable[[tuple[Any, ...], dict[str, Any], Any], str | None]


def _default_query(args: tuple[Any, ...], kwargs: dict[str, Any], output: Any) -> str | None:
    """Best-effort: stringify the first positional arg."""
    if args:
        return str(args[0])
    if kwargs:
        first = next(iter(kwargs.values()))
        return str(first)
    return None


def _format_block(name: object, response: RecallResponse) -> str:
    if not response.matches:
        return ""
    lines = [
        "<system-reminder>",
        f"Smriti recall (tool: {name}, {response.elapsed_ms}ms, "
        f"backend: {response.backend}, {len(response.matches)} relevant):",
    ]
    for m in response.matches:
        snippet = m.snippet[:240].replace("\n", " ").strip()
        lines.append(f"- {m.source} (score {m.score:.2f}): {snippet}")
    lines.append("</system-reminder>")
    return "\n".join(lines)


def wrap_tool(
    fn: Callable[..., Any],
    *,
    query_from: QueryDeriver = _default_query,
    name: str | None = None,
) -> Callable[..., ToolResult]:
    """Return a wrapped callable that fires recall after ``fn`` runs.

    ``query_from(args, kwargs, output) -> str | None``
        Derive the recall query from the call. Return None to skip
        recall for this invocation. Default: stringify the first arg.

    ``name``
        Label used in the recall block. Defaults to ``fn.__name__``.
    """
    tool_name = name or getattr(fn, "__name__", "tool")

    def wrapped(*args: Any, **kwargs: Any) -> ToolResult:
        output = fn(*args, **kwargs)
        try:
            query = query_from(args, kwargs, output)
        except Exception as exc:  # noqa: BLE001 — recall is ambient; the output stands
            log.warning("recall for %s: no query: %s", tool_name, exc)
            query = None
        if not query:
            return ToolResult(output=output)
        try:
            response = run_recall(query)
        except Exception as exc:  # noqa: BLE001 — recall is ambient; the output stands
            log.warning("recall for %s failed: %s", tool_name, exc)
            return ToolResult(output=output)
        return ToolResult(
            output=output,
            recall=response,
            recall_block=_format_block(tool_name, response),
        )

    wrapped.__name__ = f"recall_wrapped_{tool_name}"
    wrapped.__doc__ = (
        f"Recall-wrapped {tool_name}. Returns ToolResult(output, recall, "
        f"recall_block). Underlying:\n\n{fn.__doc__ or ''}"
    )
    return wrapped


__all__ = ["ToolResult", "wrap_tool", "QueryDeriver"]

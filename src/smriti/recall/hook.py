"""PostToolUse hook entry point. Wired by the per-harness installers.

Stdin payload (Claude Code or Codex):
    {"tool_name": "Read"|"Edit"|"Write"|"apply_patch"|...,
     "tool_input": { ... harness-specific shape ... }}

Tool-name handling:
    Read | Edit | Write    Claude Code edits/reads. ``tool_input.file_path``.
    apply_patch            Codex's edit tool. ``tool_input.command`` contains
                           a patch body; we parse out *** Add File: /
                           *** Update File: / *** Delete File: directives.
    Bash                   Skipped — too noisy for ambient recall (firing on
                           every ``git status`` would drown the signal).
    mcp__*                 Skipped — recursion guard. The agent calling
                           smriti_read shouldn't fire recall on itself.
    other                  Skipped silently.

Output framing controlled by ``SMRITI_RECALL_FRAMING`` env var:
    raw          (default) Plain ``<system-reminder>`` block on stdout.
                 Claude Code injects this verbatim into the assistant's
                 context.
    codex-json   {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                 "additionalContext": "..."}} JSON. Codex shape.

All errors are swallowed — a recall hook must never break the parent
tool call.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from smriti.recall.config import load_config
from smriti.recall.runner import run_recall
from smriti.recall.types import RecallMatch

_EXT_ALLOW = {
    ".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
    ".java", ".rb", ".sh", ".sql", ".toml", ".yaml", ".yml",
}

# Bytes of file content to read for query enrichment. Big enough to
# capture a docstring / opening prose, small enough that the hook
# stays fast even on large files.
_CONTENT_PROBE_BYTES = 2048

# Cap content slice contributed to the query so qmd's embedding model
# isn't fed a long chunk that drowns the stem signal.
_CONTENT_QUERY_CHARS = 400

# Codex apply_patch directive lines look like:
#   *** Add File: src/foo.py
#   *** Update File: docs/USAGE.md
#   *** Delete File: tests/old.py
_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s+(.+?)\s*$",
    re.MULTILINE,
)


def _read_head(path: Path) -> str:
    try:
        with path.open("rb") as f:
            raw = f.read(_CONTENT_PROBE_BYTES)
    except OSError:
        return ""
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter so it doesn't dominate the query."""
    if text.startswith("---\n") or text.startswith("---\r\n"):
        end = text.find("\n---", 4)
        if end != -1:
            tail = text[end + 4:]
            return tail.lstrip("\r\n")
    return text


def _content_excerpt(path: Path) -> str:
    """Excerpt the file's opening text for query enrichment."""
    head = _read_head(path)
    if not head:
        return ""
    head = _strip_frontmatter(head).strip()
    head = " ".join(head.split())
    return head[:_CONTENT_QUERY_CHARS]


def _build_query(file_path: str) -> str | None:
    """Return a recall query for ``file_path``, or None to skip.

    Skips: non-text-ish extensions, paths inside the memory tree,
    too-short stems.
    """
    p = Path(file_path)
    if p.suffix.lower() not in _EXT_ALLOW:
        return None

    # Don't recurse into the memory tree.
    narada = (Path.home() / ".narada").resolve()
    try:
        if str(p.resolve()).startswith(str(narada)):
            return None
    except OSError:
        pass

    stem = p.stem.replace("-", " ").replace("_", " ").strip()
    if len(stem) < 3:
        return None

    excerpt = _content_excerpt(p) if p.exists() else ""
    if excerpt:
        return f"{stem}: {excerpt}"
    return stem


def _extract_paths(tool_name: str, tool_input: dict) -> list[str]:
    """Return file paths to fire recall on, based on the tool call.

    Centralising this so each harness's tool_input shape is handled in
    one place. Returns ``[]`` for tools we don't fire recall on
    (Bash, MCP, unknown).
    """
    # Claude-Code-shaped tools.
    if tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path") or ""
        return [path] if path else []

    # Codex's edit tool. Patch body lives in tool_input.command.
    if tool_name == "apply_patch":
        body = tool_input.get("command") or ""
        if not isinstance(body, str):
            return []
        return list(_PATCH_FILE_RE.findall(body))

    # Bash — skip. Too noisy: every shell command would fire recall.
    # Future: opt-in extractor for cat/head/tail/less if the user wants
    # ambient recall for shell-driven file reads.
    if tool_name == "Bash":
        return []

    # MCP tools — skip to avoid the agent's smriti_read call recursing.
    if tool_name.startswith("mcp__"):
        return []

    return []


def _presence_line(payload: dict) -> str:
    """Cross-channel presence for the session firing this hook.

    Excludes the session's own channel (derived from the payload's cwd
    the same way the day-log adapter does). Deterministic and silent on
    any failure — presence must never break the parent tool call.
    """
    try:
        from smriti.daylog.adapters.claude_jsonl import channel_for
        from smriti.daylog.presence import presence_line

        cwd = str(payload.get("cwd", "") or "")
        own = channel_for(Path(cwd or "."), cwd) if cwd else ""
        return presence_line(exclude_channel=own)
    except Exception:
        return ""


def _run_recall_for_paths(paths: list[str]) -> list[RecallMatch]:
    """Aggregate recall results across a list of paths, dedup by source."""
    cfg = load_config()
    seen: dict[str, RecallMatch] = {}
    for path in paths:
        query = _build_query(path)
        if not query:
            continue
        try:
            response = run_recall(query, cfg=cfg, log_extra={"file_path": path})
        except Exception:
            continue
        for m in response.matches:
            existing = seen.get(m.source)
            if existing is None or m.score > existing.score:
                seen[m.source] = m
    # Sort by score desc, return all matches.
    return sorted(seen.values(), key=lambda m: m.score, reverse=True)


def _format_block(matches: list[RecallMatch], header: str) -> str:
    if not matches:
        return ""
    lines = [
        "<system-reminder>",
        f"Smriti recall ({header}, {len(matches)} relevant):",
    ]
    for m in matches:
        snippet = m.snippet[:240].replace("\n", " ").strip()
        lines.append(f"- {m.source} (score {m.score:.2f}): {snippet}")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"


def _frame_output(text: str, framing: str) -> str:
    if not text:
        return ""
    if framing == "raw":
        return text
    if framing == "codex-json":
        # Codex strips the <system-reminder> tags since it has its own
        # additionalContext mechanism that frames the text as developer
        # context. We pass the body without those tags.
        body = text
        body = body.replace("<system-reminder>\n", "")
        body = body.replace("\n</system-reminder>", "")
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": body.rstrip(),
            }
        })
    raise ValueError(f"unknown framing {framing!r}")


def main() -> int:
    # Recall snippets routinely contain non-ASCII (arrows, em-dashes,
    # devanagari, etc.). On Windows, Python's stdout defaults to the
    # console codepage (often cp1252), which crashes on those chars.
    # Reconfigure to UTF-8 with replacement so the hook can never die
    # on a snippet's encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0

        tool_name = payload.get("tool_name") or payload.get("toolName") or ""
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}

        paths = _extract_paths(tool_name, tool_input)
        if not paths:
            return 0

        matches = _run_recall_for_paths(paths)
        presence = _presence_line(payload)
        if not matches and not presence:
            return 0

        # Header summarises the trigger so the model can tell which tool
        # call surfaced the memory.
        if len(paths) == 1:
            header = f"tool: {tool_name}, file: {Path(paths[0]).name}"
        else:
            header = f"tool: {tool_name}, {len(paths)} files"

        block = _format_block(matches, header)
        if presence:
            if block:
                block = block.replace(
                    "</system-reminder>", f"{presence}\n</system-reminder>"
                )
            else:
                block = f"<system-reminder>\n{presence}\n</system-reminder>\n"
        framing = os.environ.get("SMRITI_RECALL_FRAMING", "raw").strip()
        sys.stdout.write(_frame_output(block, framing))
        return 0
    except Exception:
        # Honour the contract documented in the module docstring: a
        # recall hook must never break the parent tool call. Anything
        # unexpected (config parse failure, stdout I/O error, etc.)
        # collapses to a silent no-op.
        return 0


if __name__ == "__main__":
    sys.exit(main())

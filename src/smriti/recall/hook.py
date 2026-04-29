"""PostToolUse hook entry point. Wired by ``scripts/install.py``.

Stdin payload (from Claude Code):
    {"tool_name": "Read"|"Edit"|"Write",
     "tool_input": {"file_path": "..."}}

Behaviour:
    - Skip if the path is inside the memory tree (``~/.narada``) — we
      don't want recall to recurse on memory.
    - Skip non-text-ish extensions (binaries / lockfiles).
    - Build a low-noise query from the file's stem (filename without
      extension, with separators replaced by spaces).
    - Dispatch to the configured backend via ``run_recall``.
    - Emit a ``<system-reminder>`` block on stdout if any match scored
      above the threshold; otherwise stay silent.

All errors are swallowed — a recall hook must never break the parent
tool call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from smriti.recall.config import load_config
from smriti.recall.runner import run_recall

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
    # Collapse whitespace so the query stays compact.
    head = " ".join(head.split())
    return head[:_CONTENT_QUERY_CHARS]


def _build_query(file_path: str) -> str | None:
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
        # Stem first so it carries weight in BM25 token overlap, then
        # excerpt for semantic context.
        return f"{stem}: {excerpt}"
    return stem


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}

    if tool_name not in ("Read", "Edit", "Write"):
        return 0

    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return 0

    query = _build_query(file_path)
    if not query:
        return 0

    cfg = load_config()
    try:
        response = run_recall(
            query,
            cfg=cfg,
            log_extra={"tool": tool_name, "file_path": file_path},
        )
    except Exception:
        # Hook errors must never break the parent tool call.
        return 0

    if not response.matches:
        return 0

    p = Path(file_path)
    lines = [
        "<system-reminder>",
        f"Smriti recall (file: {p.name}, {response.elapsed_ms}ms, "
        f"backend: {response.backend}, {len(response.matches)} relevant):",
    ]
    for m in response.matches:
        snippet = m.snippet[:240].replace("\n", " ").strip()
        lines.append(f"- {m.source} (score {m.score:.2f}): {snippet}")
    lines.append("</system-reminder>")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

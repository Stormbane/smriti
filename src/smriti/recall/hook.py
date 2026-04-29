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

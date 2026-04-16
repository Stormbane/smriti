"""Minimal MCP server for smriti — no external dependencies.

Implements the MCP JSON-RPC protocol directly over stdio. Exposes two tools:
- smriti_read — hybrid search over the narada memory tree
- smriti_status — index statistics
- smriti_write — write to narada memory

Run via stdio::

    python -m smriti.mcp_server

Configure in .mcp.json::

    {
      "mcpServers": {
        "smriti": {
          "command": "python",
          "args": ["-m", "smriti.mcp_server"]
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# ── Tool definitions ─────────────────────────────────────────────────

TOOLS = [
    {
        "name": "smriti_read",
        "description": (
            "Search Narada's memory tree — identity files, concept wiki, "
            "goals, threads, journal, and event indexes. Returns ranked "
            "results with source path, heading, content preview, relevance "
            "score, and trunk distance (0 = identity-adjacent, higher = "
            "further from trunk)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "smriti_status",
        "description": "Show smriti index statistics.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "smriti_write",
        "description": (
            "Write a new memory entry to the narada tree. Content is saved as a "
            "dated markdown file under the specified branch (default: journal) and "
            "immediately indexed so it is searchable via smriti_read. "
            "This is the v0.1 write path — no JUDGE step yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The text to store (plain markdown).",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch under the tree root (default: journal).",
                    "default": "journal",
                },
                "title": {
                    "type": "string",
                    "description": "Optional entry title / heading.",
                },
                "source": {
                    "type": "string",
                    "description": "Provenance label (e.g. 'heartbeat', 'manual', session UUID).",
                },
            },
            "required": ["content"],
        },
    },
]

# ── Lazy DB ──────────────────────────────────────────────────────────

_db = None


def _get_db():
    global _db
    if _db is not None:
        return _db
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import ensure_schema

    db_path = smriti_db_path()
    if not db_path.exists():
        raise RuntimeError("No index. Run 'smriti index' first.")
    tmp = sqlite3.connect(str(db_path))
    row = tmp.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    tmp.close()
    if not row:
        raise RuntimeError("Index corrupted. Run 'smriti index --full'.")
    _db = ensure_schema(db_path, int(row[0]))
    return _db


# ── Tool handlers ────────────────────────────────────────────────────


def handle_read(arguments: dict) -> str:
    from smriti.store.search import search

    query = arguments.get("query", "")
    top_k = arguments.get("top_k", 5)
    if not query.strip():
        return "Error: empty query"

    db = _get_db()
    results = search(db, query, top_k=top_k, use_reranker=False)
    if not results:
        return f"No results for: {query}"

    lines = []
    for i, r in enumerate(results, 1):
        heading = f" :: {r.heading}" if r.heading else ""
        lines.append(f"[{i}] {r.source}{heading} (score: {r.score:.2f}, depth: {r.trunk_distance})")
        content = r.content.strip()
        if len(content) > 1000:
            content = content[:1000] + "\n... (truncated)"
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def handle_write(arguments: dict) -> str:
    from smriti.store.writer import write_entry

    content = arguments.get("content", "")
    if not content.strip():
        return "Error: content is empty."

    path = write_entry(
        content,
        branch=arguments.get("branch", "journal"),
        title=arguments.get("title") or None,
        source_hint=arguments.get("source") or None,
        reindex=True,
    )
    return f"Written: {path}"


def handle_status() -> str:
    from smriti.core.tree import smriti_db_path, tree_root

    db_path = smriti_db_path()
    root = tree_root()
    lines = [f"Tree root: {root}", f"Database: {db_path}"]
    if not db_path.exists():
        lines.append("Status: Not indexed")
        return "\n".join(lines)
    conn = sqlite3.connect(str(db_path))
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    files = conn.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
    model = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
    dim = conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone()
    conn.close()
    lines.extend([
        f"Files: {files}",
        f"Chunks: {chunks}",
        f"Model: {model[0] if model else '?'} (dim={dim[0] if dim else '?'})",
        f"Indexed: {last[0] if last else 'never'}",
    ])
    return "\n".join(lines)


# ── JSON-RPC over stdio ─────────────────────────────────────────────

SERVER_INFO = {
    "name": "smriti",
    "version": "0.1.0",
}

CAPABILITIES = {
    "tools": {},
}


def _make_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _make_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    method = msg.get("method", "")
    id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        return _make_response(id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": CAPABILITIES,
        })

    elif method == "notifications/initialized":
        return None  # notification, no response

    elif method == "tools/list":
        return _make_response(id, {"tools": TOOLS})

    elif method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            if name == "smriti_read":
                text = handle_read(arguments)
            elif name == "smriti_write":
                text = handle_write(arguments)
            elif name == "smriti_status":
                text = handle_status()
            else:
                return _make_error(id, -32601, f"Unknown tool: {name}")
            return _make_response(id, {
                "content": [{"type": "text", "text": text}],
            })
        except Exception as exc:
            return _make_response(id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })

    elif method == "ping":
        return _make_response(id, {})

    elif method.startswith("notifications/"):
        return None  # ignore notifications

    else:
        if id is not None:
            return _make_error(id, -32601, f"Method not found: {method}")
        return None


def main() -> None:
    """Run the MCP server over stdio."""
    # UTF-8 for Windows
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_message(msg)
        if response is not None:
            out = json.dumps(response) + "\n"
            sys.stdout.write(out)
            sys.stdout.flush()


if __name__ == "__main__":
    main()

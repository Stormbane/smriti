"""Memory tools: search, write, status, and the handoff to the next session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from smriti.core.frontmatter import one_line
from smriti.mcp.tool import Arguments, Tool, ToolError

if TYPE_CHECKING:
    from smriti.store.schema import IndexDB

_db: IndexDB | None = None


def _get_db() -> IndexDB:
    """Read-only handle for search. The MCP server never does schema
    work: ensure_schema WRITES (meta upserts, DDL, an FTS write-probe),
    and with one server per Claude/codex session those writes deadlocked
    reads across the fleet ("database is locked", 2026-08-27). The
    indexer owns the schema; this process only reads it."""
    global _db
    if _db is not None:
        return _db
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import open_readonly

    db_path = smriti_db_path()
    if not db_path.exists():
        raise ToolError("no index yet; run 'smriti index' first")
    db = open_readonly(db_path)
    row = db.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    if not row:
        db.close()
        raise ToolError("the index has no dimension recorded; run 'smriti index --full'")
    _db = db
    return _db


def handle_read(arguments: Arguments) -> str:
    from smriti.store.search import search

    query = arguments.get("query", "")
    top_k = arguments.get("top_k", 5)
    if not query.strip():
        raise ToolError("empty query")

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


def handle_write(arguments: Arguments) -> str:
    from smriti.store.writer import write_entry

    content = arguments.get("content", "")
    if not content.strip():
        raise ToolError("content is empty.")

    path = write_entry(
        content,
        branch=arguments.get("branch", "journal"),
        title=one_line(arguments.get("title") or "") or None,
        source_hint=one_line(arguments.get("source") or "") or None,
        reindex=True,
    )
    return f"Written: {path}"


def handle_status(arguments: Arguments) -> str:
    from smriti.core.tree import smriti_db_path, tree_root
    from smriti.store.schema import open_readonly

    db_path = smriti_db_path()
    root = tree_root()
    lines = [f"Tree root: {root}", f"Database: {db_path}"]
    if not db_path.exists():
        lines.append("Status: Not indexed")
        return "\n".join(lines)
    db = open_readonly(db_path)
    try:
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = db.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        model = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        dim = db.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
        last = db.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone()
    finally:
        db.close()
    lines.extend([
        f"Files: {files}",
        f"Chunks: {chunks}",
        f"Model: {model[0] if model else '?'} (dim={dim[0] if dim else '?'})",
        f"Indexed: {last[0] if last else 'never'}",
    ])
    return "\n".join(lines)


def handle_write_handoff(arguments: Arguments) -> str:
    """Write the single-slot handoff doc consumed by wake on next session start.

    Path: ~/.narada/.smriti/handoff-pending.md (single slot — overwrites).
    Wake archives it to ~/.narada/.smriti/handoff-consumed/{utc-iso}.md after
    reading, so subsequent sessions don't see the same handoff twice.
    """
    from datetime import datetime

    from smriti.core.tree import tree_root

    content = arguments.get("content", "").strip()
    summary = one_line(arguments.get("summary", ""))
    if not content:
        raise ToolError("content is required.")

    root = tree_root()
    handoff_dir = root / ".smriti"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    path = handoff_dir / "handoff-pending.md"
    overwriting = path.exists()

    now_local = datetime.now().astimezone()
    fm_lines = [
        "---",
        "type: handoff",
        f"written: {now_local.strftime('%Y-%m-%d %H:%M:%S %z')}",
    ]
    if summary:
        fm_lines.append(f"summary: {summary}")
    fm_lines.append("---")
    fm = "\n".join(fm_lines) + "\n\n"

    body = "# Handoff to next session\n\n" + content.rstrip() + "\n"
    # Under the handoff lock the wake shares, and atomically, so a wake never
    # archives a letter it didn't show or reads a half-written one.
    from smriti.wake.briefing import handoff_lock

    from smriti.core.files import FileBusy, write_atomic

    with handoff_lock(root, wait_s=5.0) as held:
        if not held:
            raise ToolError("a session is reading the handoff right now; nothing was written. Try again.")
        try:
            write_atomic(path, fm + body)
        except FileBusy:
            raise ToolError("the handoff file was busy; nothing was written. Try again.")

    msg = f"Handoff written: {path}"
    if overwriting:
        msg += " (overwrote previous handoff — single-slot)"
    if summary:
        msg += f"\nSummary: {summary}"
    msg += "\nNext session's wake will load this at the top of the briefing, then archive it."
    return msg


TOOLS = (
    Tool(
        name="smriti_read",
        description="Search {agent}'s memory tree — identity files, concept wiki, "
            "goals, threads, journal, and event indexes. Returns ranked "
            "results with source path, heading, content preview, relevance "
            "score, and trunk distance (0 = identity-adjacent, higher = "
            "further from trunk).",
        input_schema={
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
        handler=handle_read,
    ),
    Tool(
        name="smriti_write",
        description="Write a new memory entry to the {agent_key} tree. Content is saved as a "
            "dated markdown file under the specified branch (default: journal) and "
            "immediately indexed so it is searchable via smriti_read. "
            "This is the v0.1 write path — no JUDGE step yet.",
        input_schema={
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
        handler=handle_write,
    ),
    Tool(
        name="smriti_status",
        description="Show smriti index statistics.",
        input_schema={"type": "object", "properties": {}},
        handler=handle_status,
    ),
    Tool(
        name="smriti_write_handoff",
        description="Write a handoff document for the next session. Use this when "
            "you sense context pressure, hit a clean breakpoint mid-task, "
            "or otherwise want future-self to pick up exactly where you "
            "left off. The wake loader on the next session reads the "
            "handoff at the very top of the briefing, then archives it "
            "(single-slot — writing again overwrites). {user} still triggers "
            "/clear; this prepares the continuation context so neither of "
            "you has to write a re-entry prompt by hand. Good handoffs "
            "name: what was being done, what the next concrete step is, "
            "what state lives where, and what NOT to redo.",
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The handoff body, markdown. Be specific — file paths, "
                        "function names, decisions made this session, what to "
                        "skip. The next instance will see this before anything "
                        "else, but it has no other memory of this session."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Optional one-line summary stored in frontmatter for "
                        "logs / archive review. Not shown in the wake briefing."
                    ),
                },
            },
            "required": ["content"],
        },
        handler=handle_write_handoff,
    ),
)

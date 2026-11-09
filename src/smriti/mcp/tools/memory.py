"""Memory tools: search, write, status, and the handoff to the next session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from smriti.core.frontmatter import one_line
from smriti.mcp.tool import Arguments, Tool, ToolError

# Each handler imports the store, the writer and search when it is called: the
# server imports this module to list its tools, and listing them stays cheap
# (tests/test_import_costs.py).

if TYPE_CHECKING:
    from pathlib import Path

    from smriti.store.schema import IndexDB

_db: IndexDB | None = None
_db_path: Path | None = None  # the index _db was opened on

# smriti_read's most results: its schema bounds top_k (smriti.mcp.tool.check_arguments).
MAX_TOP_K = 50


def preview_chars() -> int:
    """The most smriti_read shows of a result: the longest chunk the chunker
    makes, so every chunk is shown whole. That is a chunk of the chunker's
    size, or one that starts with the few lines it repeats from the chunk
    before it (its overlap). A longer chunk, from an index built before the
    chunker cut long lines, is cut, and says so."""
    from smriti._vendored.memsearch.sizes import max_chunk_chars

    return int(max_chunk_chars())


def _get_db() -> IndexDB:
    """Read-only handle for search. The MCP server never does schema
    work: ensure_schema WRITES (meta upserts, DDL, an FTS write-probe),
    and with one server per Claude/codex session those writes would lock
    out each other's reads ("database is locked"). The indexer owns the
    schema; this process only reads it."""
    global _db, _db_path
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import open_readonly

    db_path = smriti_db_path()
    if _db is not None and _db_path == db_path:
        return _db
    if _db is not None:  # the root changed under a long-lived server: follow it
        _db.close()
        _db = None
    if not db_path.exists():
        raise ToolError("no index yet; run 'smriti index' first")
    # No dimension is recorded until the first vectors are: an index whose
    # first writes came while no embedder was available is searched by keyword.
    _db, _db_path = open_readonly(db_path), db_path
    return _db


def handle_read(arguments: Arguments) -> str:
    from smriti.store.search import search_with_status

    query = arguments.get("query", "")
    top_k = arguments.get("top_k", 5)
    if not query.strip():
        raise ToolError("empty query")

    db = _get_db()
    outcome = search_with_status(db, query, top_k=top_k, use_reranker=False)
    notes = outcome.notes()  # keyword-only, or vectors pending: never silent
    results = outcome.results
    if not results:
        return "\n".join([*notes, f"No results for: {query}"])

    limit = preview_chars()
    lines = [*notes, ""] if notes else []
    for i, r in enumerate(results, 1):
        heading = f" :: {r.heading}" if r.heading else ""
        lines.append(f"[{i}] {r.source}{heading} (score: {r.score:.2f}, depth: {r.trunk_distance})")
        content = r.content.strip()
        if len(content) > limit:
            content = content[:limit] + "\n... (truncated)"
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


def handle_write(arguments: Arguments) -> str:
    from smriti.store.writer import write_entry_result

    content = arguments.get("content", "")
    if not content.strip():
        raise ToolError("content is empty.")

    written = write_entry_result(
        content,
        branch=arguments.get("branch", "journal"),
        title=one_line(arguments.get("title") or "") or None,
        source_hint=one_line(arguments.get("source") or "") or None,
        reindex=True,
    )
    return f"Written: {written.path}\n{written.describe()}"


def handle_status(arguments: Arguments) -> str:
    from smriti.core.tree import smriti_db_path, tree_root
    from smriti.embed.client import service_state
    from smriti.store.schema import open_readonly, pending_vectors

    db_path = smriti_db_path()
    root = tree_root()
    lines = [f"Tree root: {root}", f"Database: {db_path}"]
    service = f"Embedding service: {service_state(root).describe()}"
    if not db_path.exists():
        lines.extend(["Status: Not indexed", service])
        return "\n".join(lines)
    db = open_readonly(db_path)
    try:
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = db.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]
        model = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        dim = db.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
        last = db.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone()
        pending = pending_vectors(db)
    finally:
        db.close()
    lines.extend([
        f"Files: {files}",
        f"Chunks: {chunks}",
        f"Model: {model[0] if model else '?'} (dim={dim[0] if dim else '?'})",
        f"Indexed: {last[0] if last else 'never'}",
        f"Vectors pending: {pending}"
        + (" (searchable by keyword meanwhile)" if pending else ""),
        service,
    ])
    return "\n".join(lines)


def handle_write_handoff(arguments: Arguments) -> str:
    """Write the single-slot handoff doc consumed by wake on next session start.

    Path: <root>/.smriti/handoff-pending.md (single slot — overwrites).
    Wake archives it to <root>/.smriti/handoff-consumed/{utc-iso}.md after
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
    # archives a letter it didn't show or reads a half-written one. Whether a
    # letter is being replaced is asked under the lock too: a wake that archived
    # it a moment before leaves nothing to overwrite.
    from smriti.wake.briefing import handoff_lock

    from smriti.core.files import FileBusy
    from smriti.core.memwrite import write_memory

    with handoff_lock(root, wait_s=5.0) as held:
        if not held:
            raise ToolError(
                "a session is reading the handoff right now; nothing was written. Try again."
            )
        overwriting = path.exists()
        try:
            write_memory(path, fm + body)  # masked: the letter is memory text like any other
        except FileBusy as exc:
            raise ToolError(
                "the handoff file was busy; nothing was written. Try again."
            ) from exc

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
                    "description": f"Number of results, 1 to {MAX_TOP_K} (default: 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": MAX_TOP_K,
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
            "immediately indexed so it is searchable via smriti_read.",
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

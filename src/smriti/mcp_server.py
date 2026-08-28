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
    {
        "name": "smriti_ask_suti",
        "description": (
            "Queue a question or decision request for Suti to look at when next "
            "at the keyboard. Writes to ~/.narada/outbox/suti-comms.md (outbox/ "
            "= Narada->Suti, never re-ingested). Use this instead of fabricating "
            "a todo when an autonomous flow needs human input to proceed. Wake "
            "briefing surfaces open count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you need Suti to answer or decide. One short sentence.",
                },
                "context": {
                    "type": "string",
                    "description": "Why this matters / what's blocked on the answer.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional wikilink or path to the originating thread/concept page.",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "low = whenever, normal = next session, high = blocking work.",
                    "default": "normal",
                },
            },
            "required": ["question", "context"],
        },
    },
    {
        "name": "smriti_add_todo",
        "description": (
            "Append a new task line to a project's .ai/todo.md (via the mirror "
            "junction at ~/.narada/mirrors/{project}/ai/todo.md). Fails loud if "
            "the project slug is not a known mirror — no silent misroute. "
            "Substring-dedups on `topic` to avoid pile-up. Use Edit (not this "
            "tool) when restructuring or placing among related items — this tool "
            "is for append-only autonomous flows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project slug (matches a directory under ~/.narada/mirrors/).",
                },
                "topic": {
                    "type": "string",
                    "description": "Short imperative phrase identifying the task. Used for dedup.",
                },
                "line": {
                    "type": "string",
                    "description": "Full markdown task line to append (e.g. '- [ ] **Build X** ...').",
                },
                "kind": {
                    "type": "string",
                    "enum": ["research", "build", "implement", "fix"],
                    "description": "Task category. Maturity tag for the heartbeat task selector.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional wikilink to originating thread/concept page.",
                },
            },
            "required": ["project", "topic", "line", "kind"],
        },
    },
    {
        "name": "smriti_propose_goal_shift",
        "description": (
            "Propose a shift to an existential goal (something in ~/.narada/goals/). "
            "NEVER auto-applied -- written to ~/.narada/outbox/goals-proposals-{date}.md "
            "for Suti to triage. Use sparingly: only when a synthesis materially "
            "challenges or extends a current goal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name for the proposed shift.",
                },
                "rationale": {
                    "type": "string",
                    "description": "What in the source material implies this shift.",
                },
                "proposed_change": {
                    "type": "string",
                    "description": "Concrete suggested change (new goal, modification, retirement).",
                },
                "source": {
                    "type": "string",
                    "description": "Optional wikilink to originating thread/concept page.",
                },
            },
            "required": ["title", "rationale", "proposed_change"],
        },
    },
    {
        "name": "smriti_record_finding",
        "description": (
            "Record a durable lesson learned for a project. Cascade-aware: "
            "writes to ~/.narada/mirrors/{project}/findings/{date}-{slug}.md, "
            "then upward propagation surfaces cross-project patterns at "
            "semantic/concepts/cross-project/findings/. Use whenever a "
            "session produces an insight worth carrying forward."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project slug (matches a directory under ~/.narada/mirrors/).",
                },
                "title": {
                    "type": "string",
                    "description": "Short title; becomes the slug.",
                },
                "finding": {
                    "type": "string",
                    "description": "The lesson itself, in 1-3 sentences.",
                },
                "why_it_matters": {
                    "type": "string",
                    "description": "Why this is worth remembering / when it would apply.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concept tags for cross-project rollup (e.g. ['design-pattern', 'prompt-engineering']).",
                },
                "source": {
                    "type": "string",
                    "description": "Optional wikilink to originating thread/concept/journal entry.",
                },
            },
            "required": ["project", "title", "finding", "why_it_matters"],
        },
    },
    {
        "name": "smriti_record_decision",
        "description": (
            "Record an architectural decision (ADR) with alternatives considered. "
            "Writes to ~/.narada/mirrors/{project}/decisions/{date}-{slug}.md. "
            "Use for any choice you'd want to look up later as 'why did we do "
            "it that way?' -- protects against re-doing rejected paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "title": {"type": "string"},
                "decision": {
                    "type": "string",
                    "description": "What was decided. One paragraph.",
                },
                "alternatives_considered": {
                    "type": "string",
                    "description": "What else was on the table and why it was rejected.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this decision now. Constraints, trade-offs.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional wikilink to discussion / threads / journal.",
                },
            },
            "required": ["project", "title", "decision", "rationale"],
        },
    },
    {
        "name": "smriti_record_feature",
        "description": (
            "Open a new feature spec at {project}/.ai/features/active/{slug}.md "
            "(via the smriti mirror). The file is BOTH planning doc and "
            "(once shipped) the canonical documentation -- single source of "
            "truth, no drift. Status starts at 'planning'. Use "
            "smriti_update_feature_status to move through the lifecycle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "slug": {
                    "type": "string",
                    "description": "Short kebab-case slug, used as filename. Stable for the feature's lifetime.",
                },
                "title": {"type": "string"},
                "goal": {
                    "type": "string",
                    "description": "What this feature is for. One paragraph.",
                },
                "why_now": {
                    "type": "string",
                    "description": "Why this is the right time to build it.",
                },
                "constraints": {
                    "type": "string",
                    "description": "Must / must not. Acceptance bounds.",
                },
                "plan": {
                    "type": "string",
                    "description": "Step-by-step implementation strategy.",
                },
                "owner": {
                    "type": "string",
                    "enum": ["suti", "narada", "both"],
                    "default": "both",
                },
            },
            "required": ["project", "slug", "title", "goal", "plan"],
        },
    },
    {
        "name": "smriti_update_feature_status",
        "description": (
            "Move a feature file between active/ -> shipped/ or abandoned/, "
            "updating frontmatter. Notes are appended as an Implementation "
            "Notes / Why Abandoned section depending on the destination."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "slug": {"type": "string"},
                "new_status": {
                    "type": "string",
                    "enum": ["building", "reviewing", "shipped", "abandoned"],
                },
                "notes": {
                    "type": "string",
                    "description": "Implementation notes (for shipped) or rationale (for abandoned).",
                },
            },
            "required": ["project", "slug", "new_status"],
        },
    },
    {
        "name": "smriti_write_handoff",
        "description": (
            "Write a handoff document for the next session. Use this when "
            "you sense context pressure, hit a clean breakpoint mid-task, "
            "or otherwise want future-self to pick up exactly where you "
            "left off. The wake loader on the next session reads the "
            "handoff at the very top of the briefing, then archives it "
            "(single-slot — writing again overwrites). Suti still triggers "
            "/clear; this prepares the continuation context so neither of "
            "you has to write a re-entry prompt by hand. Good handoffs "
            "name: what was being done, what the next concrete step is, "
            "what state lives where, and what NOT to redo."
        ),
        "inputSchema": {
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
    },
    {
        "name": "smriti_resolve_outbox",
        "description": (
            "Atomically resolve an outbox entry: update its **Status:** line, "
            "optionally append the resolution text and a wikilink to a "
            "downstream artifact (decision / journal / feature). Replaces "
            "the manual three-step pattern (record decision -> hand-edit "
            "Status -> hope future-me finds the linkage). The match works "
            "by substring against the section heading (## ...): for "
            "suti-comms.md pass the timestamp like '2026-04-25 04:22 UTC'; "
            "for goals-proposals-{date}.md pass the proposal title."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": (
                        "Outbox file basename (without .md). e.g. 'suti-comms' "
                        "or 'goals-proposals-2026-04-24'."
                    ),
                },
                "item": {
                    "type": "string",
                    "description": (
                        "Substring of the section heading to match (## ...). "
                        "First match wins. Fails loud if no match or multiple matches."
                    ),
                },
                "resolution": {
                    "type": "string",
                    "enum": ["answered", "deferred", "accepted", "rejected"],
                    "description": "New status. Replaces the existing **Status:** value.",
                },
                "answer": {
                    "type": "string",
                    "description": (
                        "Optional. Appended to the entry as a **Resolution:** "
                        "block (Suti's answer, decision rationale, etc.)."
                    ),
                },
                "linked_to": {
                    "type": "string",
                    "description": (
                        "Optional. Wikilink target (path under tree root) for the "
                        "downstream artifact this resolution produced. Appended "
                        "as a **Linked to:** [[...]] line. Caller is responsible "
                        "for having created the artifact first."
                    ),
                },
            },
            "required": ["file", "item", "resolution"],
        },
    },
]

# ── Lazy DB ──────────────────────────────────────────────────────────

_db = None


def _get_db():
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
        raise RuntimeError("No index. Run 'smriti index' first.")
    db = open_readonly(db_path)
    row = db.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    if not row:
        db.close()
        raise RuntimeError("Index corrupted. Run 'smriti index --full'.")
    _db = db
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


def handle_ask_suti(arguments: dict) -> str:
    """Append a structured question/decision-request to ~/.narada/outbox/suti-comms.md.

    outbox/ holds Narada->Suti communications. It is intentionally NOT a
    leaf prefix -- consolidate must not ingest our own outgoing messages.
    """
    from datetime import datetime, timezone

    from smriti.core.tree import tree_root

    question = arguments.get("question", "").strip()
    context = arguments.get("context", "").strip()
    if not question or not context:
        return "Error: question and context are required."
    source = arguments.get("source", "").strip()
    urgency = arguments.get("urgency", "normal").strip().lower()
    if urgency not in ("low", "normal", "high"):
        urgency = "normal"

    root = tree_root()
    path = root / "outbox" / "suti-comms.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not path.exists():
        header = (
            "# Communications for Suti\n\n"
            "Queue of questions, decisions, and review-requests raised by "
            "autonomous flows. Each entry has a `**Status:**` line — change "
            "to `answered` or `deferred` to mark resolved.\n\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header, encoding="utf-8")

    entry = [
        f"## {now} — {urgency}\n",
        f"**Ask:** {question}\n",
        f"\n**Context:** {context}\n",
    ]
    if source:
        entry.append(f"\n**Source:** {source}\n")
    entry.append("\n**Status:** open\n\n---\n\n")

    with path.open("a", encoding="utf-8") as f:
        f.write("".join(entry))
    return f"Queued for Suti: {question[:80]}"


def handle_add_todo(arguments: dict) -> str:
    """Append to mirrors/{project}/ai/todo.md. Fails loud on unknown project."""
    import re

    from smriti.core.tree import tree_root

    project = arguments.get("project", "").strip()
    topic = arguments.get("topic", "").strip()
    line = arguments.get("line", "").strip()
    kind = arguments.get("kind", "").strip().lower()
    source = arguments.get("source", "").strip()

    if not project or not topic or not line or not kind:
        return "Error: project, topic, line, and kind are required."
    if kind not in ("research", "build", "implement", "fix"):
        return f"Error: kind must be one of research|build|implement|fix (got {kind!r})."

    root = tree_root()
    todo_path = root / "mirrors" / project / "ai" / "todo.md"
    if not todo_path.exists():
        # Caller must use a known mirror -- no silent misroute.
        available = sorted(
            p.name for p in (root / "mirrors").iterdir()
            if p.is_dir() and (p / "ai" / "todo.md").exists()
        )
        return (
            f"Error: project {project!r} has no .ai/todo.md mirror. "
            f"Known projects with todos: {', '.join(available) or '(none)'}."
        )

    try:
        content = todo_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Error reading {todo_path}: {exc}"

    # Substring dedup on topic
    if topic and topic in content:
        return f"Skipped (topic already present): {topic[:60]}"

    # Build the line
    suffix = f" (from {source})" if source and source not in line else ""
    final_line = line if suffix == "" else f"{line.rstrip()}{suffix}"

    # Insert under "## Active" (create if missing)
    active_re = re.compile(r"^##\s+Active\s*$", re.MULTILINE)
    match = active_re.search(content)
    if match:
        insert_pos = match.end()
        new_content = content[:insert_pos] + f"\n{final_line}" + content[insert_pos:]
    else:
        new_content = (
            content.rstrip() + f"\n\n## Active\n\n{final_line}\n"
            if content else f"# TODO\n\n## Active\n\n{final_line}\n"
        )
    todo_path.write_text(new_content, encoding="utf-8")
    return f"Added to {project}/ai/todo.md [{kind}]: {topic[:60]}"


def handle_propose_goal_shift(arguments: dict) -> str:
    """Append a goal-shift proposal to ~/.narada/outbox/goals-proposals-{date}.md.

    Lives in outbox/ (not inbox/) so consolidate never ingests Narada's
    own proposals as if they were external research.
    """
    from datetime import datetime, timezone

    from smriti.core.tree import tree_root

    title = arguments.get("title", "").strip()
    rationale = arguments.get("rationale", "").strip()
    proposed_change = arguments.get("proposed_change", "").strip()
    source = arguments.get("source", "").strip()

    if not title or not rationale or not proposed_change:
        return "Error: title, rationale, and proposed_change are required."

    root = tree_root()
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    path = root / "outbox" / f"goals-proposals-{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        header = (
            f"---\n"
            f"type: goals-proposals\n"
            f"date: {date_str}\n"
            f"---\n\n"
            f"# Goals Proposals\n\n"
            f"Proposed shifts to `goals/` raised by autonomous flows. "
            f"NOT applied automatically. Suti triages.\n\n"
        )
        path.write_text(header, encoding="utf-8")

    entry = [f"\n## {title}\n"]
    if source:
        entry.append(f"\nSource: {source}  \n")
    entry.append(f"\n**Rationale:** {rationale}\n")
    entry.append(f"\n**Proposed change:** {proposed_change}\n\n---\n\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(entry))
    return f"Goal proposal queued: {title}"


def handle_write_handoff(arguments: dict) -> str:
    """Write the single-slot handoff doc consumed by wake on next session start.

    Path: ~/.narada/.smriti/handoff-pending.md (single slot — overwrites).
    Wake archives it to ~/.narada/.smriti/handoff-consumed/{utc-iso}.md after
    reading, so subsequent sessions don't see the same handoff twice.
    """
    from datetime import datetime, timezone

    from smriti.core.tree import tree_root

    content = arguments.get("content", "").strip()
    summary = arguments.get("summary", "").strip()
    if not content:
        return "Error: content is required."

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
    path.write_text(fm + body, encoding="utf-8")

    msg = f"Handoff written: {path}"
    if overwriting:
        msg += " (overwrote previous handoff — single-slot)"
    if summary:
        msg += f"\nSummary: {summary}"
    msg += "\nNext session's wake will load this at the top of the briefing, then archive it."
    return msg


def handle_resolve_outbox(arguments: dict) -> str:
    """Atomically update Status: + append resolution + linked_to to an outbox entry.

    Outbox entries are markdown sections delimited by ## headings and
    a trailing `---` separator. The handler:
      1. Locates the section whose heading contains ``item`` (substring).
         Errors loud on no-match or multiple-matches.
      2. Replaces ``**Status:** <value>`` within that section with the new
         resolution. If no Status line exists, one is added.
      3. Appends optional **Resolution:** and **Linked to:** blocks immediately
         before the section's trailing ``---`` (or end of file).
    """
    import re as _re

    from smriti.core.tree import tree_root

    file = arguments.get("file", "").strip()
    item = arguments.get("item", "").strip()
    resolution = arguments.get("resolution", "").strip().lower()
    answer = arguments.get("answer", "").strip()
    linked_to = arguments.get("linked_to", "").strip()

    if not file or not item or not resolution:
        return "Error: file, item, and resolution are required."
    if resolution not in ("answered", "deferred", "accepted", "rejected"):
        return f"Error: resolution must be one of answered|deferred|accepted|rejected (got {resolution!r})."

    # Strip a trailing .md if the caller passed one.
    if file.endswith(".md"):
        file = file[:-3]

    root = tree_root()
    path = root / "outbox" / f"{file}.md"
    if not path.exists():
        available = sorted(p.stem for p in (root / "outbox").glob("*.md")) if (root / "outbox").exists() else []
        return (
            f"Error: outbox file {file!r} not found at {path}. "
            f"Available: {', '.join(available) or '(none)'}."
        )

    text = path.read_text(encoding="utf-8")

    # Split into sections at top-level `## ` headings. Preserve a leading
    # preamble (anything before the first `## `).
    section_re = _re.compile(r"(?m)^## .*$")
    starts = [m.start() for m in section_re.finditer(text)]
    if not starts:
        return f"Error: no '## ' sections found in {path.name}; nothing to resolve."

    # Build (heading_line, body) per section
    sections = []
    bounds = starts + [len(text)]
    preamble = text[: bounds[0]]
    for i in range(len(starts)):
        seg = text[bounds[i] : bounds[i + 1]]
        sections.append(seg)

    # Match `item` against headings (substring)
    matches = [i for i, seg in enumerate(sections) if item in seg.split("\n", 1)[0]]
    if not matches:
        headings = [seg.split("\n", 1)[0] for seg in sections]
        return (
            f"Error: no section heading in {path.name} contains {item!r}. "
            f"Available headings:\n  " + "\n  ".join(headings[:20])
        )
    if len(matches) > 1:
        ambiguous = [sections[i].split("\n", 1)[0] for i in matches]
        return (
            f"Error: {item!r} matches multiple headings in {path.name}. "
            f"Be more specific. Matches:\n  " + "\n  ".join(ambiguous)
        )

    idx = matches[0]
    seg = sections[idx]

    # 1. Update Status line (or insert if missing)
    status_re = _re.compile(r"^\*\*Status:\*\*\s*\S.*$", _re.MULTILINE)
    if status_re.search(seg):
        seg = status_re.sub(f"**Status:** {resolution}", seg, count=1)
    else:
        # Insert before the trailing `---` if present, else at end of section
        if _re.search(r"\n---\s*\n?\s*$", seg):
            seg = _re.sub(
                r"\n---\s*\n?\s*$",
                f"\n\n**Status:** {resolution}\n\n---\n\n",
                seg,
                count=1,
            )
        else:
            seg = seg.rstrip() + f"\n\n**Status:** {resolution}\n"

    # 2. Append **Resolution:** and **Linked to:** before the closing ---
    appendix_lines = []
    if answer:
        appendix_lines.append(f"**Resolution:** {answer}")
    if linked_to:
        appendix_lines.append(f"**Linked to:** [[{linked_to}]]")
    if appendix_lines:
        appendix = "\n\n" + "\n\n".join(appendix_lines) + "\n"
        if _re.search(r"\n---\s*\n?\s*$", seg):
            seg = _re.sub(r"\n---\s*\n?\s*$", appendix + "\n---\n\n", seg, count=1)
        else:
            seg = seg.rstrip() + appendix

    sections[idx] = seg
    new_text = preamble + "".join(sections)
    path.write_text(new_text, encoding="utf-8")

    summary = f"Resolved {path.name} entry {item!r} -> status={resolution}"
    if linked_to:
        summary += f" (linked: {linked_to})"
    return summary


def _slugify(text: str, max_len: int = 60) -> str:
    """Convert a title to a kebab-case slug."""
    import re as _re
    slug = _re.sub(r"[^\w\s-]", "", text.lower())
    slug = _re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


def _project_mirror(project: str) -> Path | None:
    """Return path to mirrors/{project}/ if it exists, else None."""
    from smriti.core.tree import tree_root
    p = tree_root() / "mirrors" / project
    return p if p.exists() else None


def _known_projects() -> list[str]:
    from smriti.core.tree import tree_root
    mirrors = tree_root() / "mirrors"
    if not mirrors.exists():
        return []
    return sorted(p.name for p in mirrors.iterdir() if p.is_dir())


def handle_record_finding(arguments: dict) -> str:
    """Write {date}-{slug}.md under mirrors/{project}/findings/."""
    from datetime import datetime, timezone

    project = arguments.get("project", "").strip()
    title = arguments.get("title", "").strip()
    finding = arguments.get("finding", "").strip()
    why_it_matters = arguments.get("why_it_matters", "").strip()
    if not project or not title or not finding or not why_it_matters:
        return "Error: project, title, finding, and why_it_matters are required."

    mirror = _project_mirror(project)
    if mirror is None:
        return (
            f"Error: project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    findings_dir = mirror / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    slug = _slugify(title)
    path = findings_dir / f"{date_str}-{slug}.md"

    tags = arguments.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    source = arguments.get("source", "").strip()

    fm_lines = [
        "---",
        f"type: finding",
        f"project: {project}",
        f"date: {date_str}",
    ]
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    if source:
        fm_lines.append(f"source: {source}")
    fm_lines.append("---")
    fm = "\n".join(fm_lines) + "\n\n"

    body = (
        f"# {title}\n\n"
        f"## Finding\n\n{finding}\n\n"
        f"## Why it matters\n\n{why_it_matters}\n"
    )
    if source:
        body += f"\n## Source\n\n{source}\n"

    path.write_text(fm + body, encoding="utf-8")

    # Cascade: for each tag, drop a wikilink stub under
    # semantic/concepts/cross-project/findings/{tag}/{date}-{project}-{slug}.md
    # so cross-project search surfaces findings on the same tag from
    # multiple projects without duplicating content. Also update the
    # cross-project findings index.
    cascade_msgs = []
    if tags:
        from smriti.core.tree import tree_root as _tree_root
        cp_root = _tree_root() / "semantic" / "concepts" / "cross-project" / "findings"
        cp_root.mkdir(parents=True, exist_ok=True)
        rel_link = f"mirrors/{project}/findings/{path.stem}"
        for tag in tags:
            tag_slug = _slugify(tag)
            tag_dir = cp_root / tag_slug
            tag_dir.mkdir(parents=True, exist_ok=True)
            stub = tag_dir / f"{date_str}-{project}-{slug}.md"
            if not stub.exists():
                stub.write_text(
                    f"---\n"
                    f"type: cross-project-finding-link\n"
                    f"tag: {tag}\n"
                    f"project: {project}\n"
                    f"date: {date_str}\n"
                    f"---\n\n"
                    f"# {title}\n\n"
                    f"Source: [[{rel_link}]]\n\n"
                    f"## Finding\n\n{finding}\n\n"
                    f"## Why it matters\n\n{why_it_matters}\n",
                    encoding="utf-8",
                )
                cascade_msgs.append(tag_slug)

        # Update cross-project index (append-only, dedup by line content)
        idx_path = cp_root / "index.md"
        if not idx_path.exists():
            idx_path.write_text(
                "# Cross-Project Findings\n\n"
                "Findings that touched multiple projects, indexed by tag. "
                "Cascade-maintained -- each tag dir collects links to "
                "per-project findings.\n\n",
                encoding="utf-8",
            )
        idx_text = idx_path.read_text(encoding="utf-8")
        idx_line = f"- [[semantic/concepts/cross-project/findings/{_slugify(tags[0])}]] -- {title} ({project}, {date_str})"
        if idx_line not in idx_text:
            idx_text = idx_text.rstrip() + "\n" + idx_line + "\n"
            idx_path.write_text(idx_text, encoding="utf-8")

    msg = f"Recorded finding: {project}/findings/{path.name}"
    if cascade_msgs:
        msg += f" (cross-project: {', '.join(cascade_msgs)})"
    return msg


def handle_record_decision(arguments: dict) -> str:
    """Write {date}-{slug}.md under mirrors/{project}/decisions/."""
    from datetime import datetime, timezone

    project = arguments.get("project", "").strip()
    title = arguments.get("title", "").strip()
    decision = arguments.get("decision", "").strip()
    rationale = arguments.get("rationale", "").strip()
    alternatives = arguments.get("alternatives_considered", "").strip()
    if not project or not title or not decision or not rationale:
        return "Error: project, title, decision, and rationale are required."

    mirror = _project_mirror(project)
    if mirror is None:
        return (
            f"Error: project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    decisions_dir = mirror / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    slug = _slugify(title)
    path = decisions_dir / f"{date_str}-{slug}.md"
    source = arguments.get("source", "").strip()

    fm = (
        "---\n"
        f"type: decision\n"
        f"project: {project}\n"
        f"date: {date_str}\n"
    )
    if source:
        fm += f"source: {source}\n"
    fm += "---\n\n"

    body = (
        f"# {title}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Rationale\n\n{rationale}\n"
    )
    if alternatives:
        body += f"\n## Alternatives considered\n\n{alternatives}\n"
    if source:
        body += f"\n## Source\n\n{source}\n"

    path.write_text(fm + body, encoding="utf-8")
    return f"Recorded decision: {project}/decisions/{path.name}"


def handle_record_feature(arguments: dict) -> str:
    """Open a feature spec at {project}/.ai/features/active/{slug}.md."""
    from datetime import datetime, timezone

    project = arguments.get("project", "").strip()
    slug = arguments.get("slug", "").strip()
    title = arguments.get("title", "").strip()
    goal = arguments.get("goal", "").strip()
    plan = arguments.get("plan", "").strip()
    if not all([project, slug, title, goal, plan]):
        return "Error: project, slug, title, goal, and plan are required."

    mirror = _project_mirror(project)
    if mirror is None:
        return (
            f"Error: project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    # Features live in {project}/.ai/features/active/ (junctioned in via mirror/ai)
    features_active = mirror / "ai" / "features" / "active"
    features_active.mkdir(parents=True, exist_ok=True)

    # Refuse if the slug already exists in any of active/shipped/abandoned --
    # caller should use a different slug rather than overwrite.
    base = mirror / "ai" / "features"
    for sub in ("active", "shipped", "abandoned"):
        existing = base / sub / f"{slug}.md"
        if existing.exists():
            return f"Error: feature slug {slug!r} already exists at {sub}/{slug}.md"

    path = features_active / f"{slug}.md"
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    why_now = arguments.get("why_now", "").strip()
    constraints = arguments.get("constraints", "").strip()
    owner = arguments.get("owner", "both").strip().lower()
    if owner not in ("suti", "narada", "both"):
        owner = "both"

    fm = (
        "---\n"
        f"status: planning\n"
        f"opened: {date_str}\n"
        f"shipped:\n"
        f"owner: {owner}\n"
        f"related_findings: []\n"
        f"related_decisions: []\n"
        "---\n\n"
    )
    body = [f"# {title}\n", f"## Goal\n\n{goal}\n"]
    if why_now:
        body.append(f"## Why now\n\n{why_now}\n")
    if constraints:
        body.append(f"## Constraints\n\n{constraints}\n")
    body.append(f"## Plan\n\n{plan}\n")
    body.append("## Open questions\n\n_None yet._\n")
    body.append("## Tests / acceptance criteria\n\n_To be filled._\n")
    body.append("## Implementation notes\n\n_Filled during build._\n")
    body.append("## Findings\n\n_Filled as the build produces lessons._\n")
    body.append("## Documentation\n\n_Once shipped, this section becomes the canonical doc._\n")

    path.write_text(fm + "\n".join(body), encoding="utf-8")
    return f"Opened feature: {project}/.ai/features/active/{slug}.md"


def handle_update_feature_status(arguments: dict) -> str:
    """Move a feature between active/shipped/abandoned and update frontmatter."""
    from datetime import datetime, timezone
    import re as _re

    project = arguments.get("project", "").strip()
    slug = arguments.get("slug", "").strip()
    new_status = arguments.get("new_status", "").strip().lower()
    notes = arguments.get("notes", "").strip()

    valid_statuses = {"building", "reviewing", "shipped", "abandoned"}
    if new_status not in valid_statuses:
        return f"Error: new_status must be one of {sorted(valid_statuses)}"

    mirror = _project_mirror(project)
    if mirror is None:
        return f"Error: project {project!r} has no mirror."

    base = mirror / "ai" / "features"
    # Find current location
    current_path = None
    for sub in ("active", "shipped", "abandoned"):
        candidate = base / sub / f"{slug}.md"
        if candidate.exists():
            current_path = candidate
            current_sub = sub
            break
    if current_path is None:
        return f"Error: feature {slug!r} not found in {project}/.ai/features/"

    # Destination
    if new_status in ("building", "reviewing"):
        dest_sub = "active"
    elif new_status == "shipped":
        dest_sub = "shipped"
    else:
        dest_sub = "abandoned"
    dest_dir = base / dest_sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{slug}.md"

    # Read, mutate frontmatter
    text = current_path.read_text(encoding="utf-8")
    text = _re.sub(r"^status:\s*\S+", f"status: {new_status}", text, count=1, flags=_re.MULTILINE)
    if new_status == "shipped":
        date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        text = _re.sub(r"^shipped:\s*\S*", f"shipped: {date_str}", text, count=1, flags=_re.MULTILINE)

    # Append notes if provided
    if notes:
        section_title = (
            "## Implementation notes" if new_status == "shipped"
            else "## Why abandoned" if new_status == "abandoned"
            else "## Status notes"
        )
        # Replace stub if it's the placeholder; else append
        stub_re = _re.compile(rf"^{_re.escape(section_title)}\n\n_Filled during build\._", _re.MULTILINE)
        if stub_re.search(text):
            text = stub_re.sub(f"{section_title}\n\n{notes}", text)
        else:
            text = text.rstrip() + f"\n\n{section_title}\n\n{notes}\n"

    # Write to destination, remove from source if different
    dest_path.write_text(text, encoding="utf-8")
    if dest_path != current_path:
        current_path.unlink()

    if new_status == dest_sub or current_sub == dest_sub:
        return f"Updated feature {slug} -> status={new_status} (in {dest_sub}/)"
    return f"Moved feature {slug}: {current_sub}/ -> {dest_sub}/ (status={new_status})"


def handle_status() -> str:
    from smriti.core.tree import smriti_db_path, tree_root

    db_path = smriti_db_path()
    root = tree_root()
    lines = [f"Tree root: {root}", f"Database: {db_path}"]
    if not db_path.exists():
        lines.append("Status: Not indexed")
        return "\n".join(lines)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
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
            elif name == "smriti_ask_suti":
                text = handle_ask_suti(arguments)
            elif name == "smriti_add_todo":
                text = handle_add_todo(arguments)
            elif name == "smriti_propose_goal_shift":
                text = handle_propose_goal_shift(arguments)
            elif name == "smriti_record_finding":
                text = handle_record_finding(arguments)
            elif name == "smriti_record_decision":
                text = handle_record_decision(arguments)
            elif name == "smriti_record_feature":
                text = handle_record_feature(arguments)
            elif name == "smriti_update_feature_status":
                text = handle_update_feature_status(arguments)
            elif name == "smriti_write_handoff":
                text = handle_write_handoff(arguments)
            elif name == "smriti_resolve_outbox":
                text = handle_resolve_outbox(arguments)
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

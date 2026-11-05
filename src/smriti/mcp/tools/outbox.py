"""Tools that reach the user: tell now, ask, propose a goal shift, resolve an ask."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from smriti.mcp.tool import Arguments, Tool, ToolError

if TYPE_CHECKING:
    from smriti.daylog.notify import Delivery

Sender = Callable[[str, str], "Delivery"]

# Tests replace this (or pass ``sender=``) so no real message is sent.
# None means: the real sender, notify_suti.py via daylog's notify_cmd.
TELL_SUTI_SENDER: Sender | None = None


def _default_tell_suti_sender(text: str, level: str) -> Delivery:
    from smriti.daylog.config import load_config
    from smriti.daylog.notify import send

    return send(load_config(), text, level=level, source="tell_suti")


def handle_tell_suti(arguments: Arguments, sender: Sender | None = None) -> str:
    """Message Suti through every channel notify_suti.py fans out to.

    Raises on any failure so the caller sees ``isError`` rather than a
    message that silently went nowhere.
    """
    from smriti.daylog.notify import LEVELS, MAX_MESSAGE_CHARS

    text = str(arguments.get("text") or "").strip()
    level = str(arguments.get("level") or "message").strip().lower()
    if not text:
        raise ToolError("text is empty; nothing sent")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ToolError(
            f"text is {len(text)} chars; the limit is {MAX_MESSAGE_CHARS}. Split it."
        )
    if level not in LEVELS:
        raise ToolError(f"unknown level {level!r}; use one of {', '.join(LEVELS)}")
    send = sender or TELL_SUTI_SENDER or _default_tell_suti_sender
    delivery = send(text, level)
    if not delivery.ok:
        raise ToolError(f"not delivered: {delivery.detail or 'no channel took it'}")
    return f"Delivered via {', '.join(delivery.channels)} (level={level})."


def handle_ask_suti(arguments: Arguments) -> str:
    """Append a structured question/decision-request to ~/.narada/outbox/suti-comms.md.

    outbox/ holds Narada->Suti communications. It is intentionally NOT a
    leaf prefix -- consolidate must not ingest our own outgoing messages.
    """
    from datetime import datetime, timezone

    from smriti.core.files import edit_lock
    from smriti.core.tree import tree_root

    question = arguments.get("question", "").strip()
    context = arguments.get("context", "").strip()
    if not question or not context:
        raise ToolError("question and context are required.")
    source = arguments.get("source", "").strip()
    urgency = arguments.get("urgency", "normal").strip().lower()
    if urgency not in ("low", "normal", "high"):
        urgency = "normal"

    root = tree_root()
    path = root / "outbox" / "suti-comms.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        "# Communications for Suti\n\n"
        "Queue of questions, decisions, and review-requests raised by "
        "autonomous flows. Each entry has a `**Status:**` line — change "
        "to `answered` or `deferred` to mark resolved.\n\n"
    )

    entry = [
        f"## {now} — {urgency}\n",
        f"**Ask:** {question}\n",
        f"\n**Context:** {context}\n",
    ]
    if source:
        entry.append(f"\n**Source:** {source}\n")
    entry.append("\n**Status:** open\n\n---\n\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    with edit_lock(path):
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write((header if new_file else "") + "".join(entry))
    return f"Queued for Suti: {question[:80]}"


def handle_propose_goal_shift(arguments: Arguments) -> str:
    """Append a goal-shift proposal to ~/.narada/outbox/goals-proposals-{date}.md.

    Lives in outbox/ (not inbox/) so consolidate never ingests Narada's
    own proposals as if they were external research.
    """
    from datetime import datetime

    from smriti.core.files import edit_lock
    from smriti.core.tree import tree_root

    title = arguments.get("title", "").strip()
    rationale = arguments.get("rationale", "").strip()
    proposed_change = arguments.get("proposed_change", "").strip()
    source = arguments.get("source", "").strip()

    if not title or not rationale or not proposed_change:
        raise ToolError("title, rationale, and proposed_change are required.")

    root = tree_root()
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    path = root / "outbox" / f"goals-proposals-{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"---\n"
        f"type: goals-proposals\n"
        f"date: {date_str}\n"
        f"---\n\n"
        f"# Goals Proposals\n\n"
        f"Proposed shifts to `goals/` raised by autonomous flows. "
        f"NOT applied automatically. Suti triages.\n\n"
    )

    entry = [f"\n## {title}\n"]
    if source:
        entry.append(f"\nSource: {source}  \n")
    entry.append(f"\n**Rationale:** {rationale}\n")
    entry.append(f"\n**Proposed change:** {proposed_change}\n\n---\n\n")
    with edit_lock(path):
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write((header if new_file else "") + "".join(entry))
    return f"Goal proposal queued: {title}"


def handle_resolve_outbox(arguments: Arguments) -> str:
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

    from smriti.core.files import edit_lock, write_atomic
    from smriti.core.tree import tree_root

    file = arguments.get("file", "").strip()
    item = arguments.get("item", "").strip()
    resolution = arguments.get("resolution", "").strip().lower()
    answer = arguments.get("answer", "").strip()
    linked_to = arguments.get("linked_to", "").strip()

    if not file or not item or not resolution:
        raise ToolError("file, item, and resolution are required.")
    if resolution not in ("answered", "deferred", "accepted", "rejected"):
        raise ToolError(f"resolution must be one of answered|deferred|accepted|rejected (got {resolution!r}).")

    # Strip a trailing .md if the caller passed one.
    if file.endswith(".md"):
        file = file[:-3]

    root = tree_root()
    path = root / "outbox" / f"{file}.md"
    if not path.exists():
        available = sorted(p.stem for p in (root / "outbox").glob("*.md")) if (root / "outbox").exists() else []
        raise ToolError(
            f"outbox file {file!r} not found at {path}. "
            f"Available: {', '.join(available) or '(none)'}."
        )

    with edit_lock(path):
        text = path.read_text(encoding="utf-8")

        # Split into sections at top-level `## ` headings. Preserve a leading
        # preamble (anything before the first `## `).
        section_re = _re.compile(r"(?m)^## .*$")
        starts = [m.start() for m in section_re.finditer(text)]
        if not starts:
            raise ToolError(f"no '## ' sections found in {path.name}; nothing to resolve.")

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
            raise ToolError(
                f"no section heading in {path.name} contains {item!r}. "
                f"Available headings:\n  " + "\n  ".join(headings[:20])
            )
        if len(matches) > 1:
            ambiguous = [sections[i].split("\n", 1)[0] for i in matches]
            raise ToolError(
                f"{item!r} matches multiple headings in {path.name}. "
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
        write_atomic(path, new_text)

    summary = f"Resolved {path.name} entry {item!r} -> status={resolution}"
    if linked_to:
        summary += f" (linked: {linked_to})"
    return summary


TOOLS = (
    Tool(
        name="tell_suti",
        description="Send Suti a message now. It goes out through notify_suti.py, which "
            "fans out to Telegram and the phone app (feed and push), and the "
            "result names the channels that delivered. Use it whenever he asks "
            "you to message him, or when something needs his attention away "
            "from the keyboard. level: 'message' (default, conversation), "
            "'error' or 'emergency' for faults, 'log'/'warn' for the app feed "
            "only. For a question that can wait for his next session, use "
            "smriti_ask_suti instead.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The message, plain text, at most 4,000 characters.",
                },
                "level": {
                    "type": "string",
                    "enum": ["message", "error", "emergency", "warn", "log"],
                    "description": "Routing level (default: message).",
                    "default": "message",
                },
            },
            "required": ["text"],
        },
        handler=handle_tell_suti,
    ),
    Tool(
        name="smriti_ask_suti",
        description="Queue a question or decision request for Suti to look at when next "
            "at the keyboard. Writes to ~/.narada/outbox/suti-comms.md (outbox/ "
            "= Narada->Suti, never re-ingested). Use this instead of fabricating "
            "a todo when an autonomous flow needs human input to proceed. Wake "
            "briefing surfaces open count.",
        input_schema={
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
        handler=handle_ask_suti,
    ),
    Tool(
        name="smriti_propose_goal_shift",
        description="Propose a shift to an existential goal (something in ~/.narada/goals/). "
            "NEVER auto-applied -- written to ~/.narada/outbox/goals-proposals-{date}.md "
            "for Suti to triage. Use sparingly: only when a synthesis materially "
            "challenges or extends a current goal.",
        input_schema={
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
        handler=handle_propose_goal_shift,
    ),
    Tool(
        name="smriti_resolve_outbox",
        description="Atomically resolve an outbox entry: update its **Status:** line, "
            "optionally append the resolution text and a wikilink to a "
            "downstream artifact (decision / journal / feature). Replaces "
            "the manual three-step pattern (record decision -> hand-edit "
            "Status -> hope future-me finds the linkage). The match works "
            "by substring against the section heading (## ...): for "
            "suti-comms.md pass the timestamp like '2026-04-25 04:22 UTC'; "
            "for goals-proposals-{date}.md pass the proposal title.",
        input_schema={
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
        handler=handle_resolve_outbox,
    ),
)

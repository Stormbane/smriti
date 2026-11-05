"""Project tools: todos, findings, decisions and feature specs in a project mirror."""

from __future__ import annotations

import re
from pathlib import Path

from smriti.core.frontmatter import one_line
from smriti.mcp.tool import Arguments, Tool, ToolError

# Characters that would end or split the frontmatter's [tag, tag] list.
_LIST_SYNTAX = str.maketrans("[],", "   ")


def _mentions(text: str, phrase: str) -> bool:
    """Whether ``phrase`` appears in ``text`` as whole words, any case or spacing."""
    words = phrase.split()
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _slugify(text: str, max_len: int = 60) -> str:
    """Convert a title to a kebab-case slug."""
    import re as _re
    slug = _re.sub(r"[^\w\s-]", "", text.lower())
    slug = _re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


def _project_mirror(project: str) -> Path | None:
    """Return path to mirrors/{project}/ if it exists, else None."""
    from smriti.core.safe_names import safe_name
    from smriti.core.tree import tree_root
    p = tree_root() / "mirrors" / safe_name(project, "project")
    return p if p.exists() else None


def _known_projects() -> list[str]:
    from smriti.core.tree import tree_root
    mirrors = tree_root() / "mirrors"
    if not mirrors.exists():
        return []
    return sorted(p.name for p in mirrors.iterdir() if p.is_dir())


def _feature_owners() -> tuple[str, str, str]:
    """Who can own a feature: the user and the agent by their configured keys, or both.

    The same values ``smriti_record_feature``'s ``owner`` enum lists.
    """
    from smriti.daylog.config import load_config
    from smriti.mcp.tool import party_names

    cfg = load_config()
    names = party_names(cfg.user_name, cfg.agent_name)
    return names["user_key"], names["agent_key"], "both"


def handle_add_todo(arguments: Arguments) -> str:
    """Append to mirrors/{project}/ai/todo.md. Fails loud on unknown project."""
    import re

    from smriti.core.files import edit_lock, write_atomic
    from smriti.core.tree import tree_root

    project = arguments.get("project", "").strip()
    topic = arguments.get("topic", "").strip()
    line = arguments.get("line", "").strip()
    kind = arguments.get("kind", "").strip().lower()
    source = one_line(arguments.get("source", ""))

    if not project or not topic or not line or not kind:
        raise ToolError("project, topic, line, and kind are required.")
    if kind not in ("research", "build", "implement", "fix"):
        raise ToolError(f"kind must be one of research|build|implement|fix (got {kind!r}).")

    root = tree_root()
    todo_path = root / "mirrors" / project / "ai" / "todo.md"
    if not todo_path.exists():
        # Caller must use a known mirror -- no silent misroute.
        available = [p for p in _known_projects()
                     if (root / "mirrors" / p / "ai" / "todo.md").exists()]
        raise ToolError(
            f"project {project!r} has no .ai/todo.md mirror. "
            f"Known projects with todos: {', '.join(available) or '(none)'}."
        )

    suffix = f" (from {source})" if source and source not in line else ""
    final_line = line if suffix == "" else f"{line.rstrip()}{suffix}"
    active_re = re.compile(r"^##\s+Active\s*$", re.MULTILINE)

    with edit_lock(todo_path):
        try:
            content = todo_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"could not read {todo_path}: {exc}") from exc

        # The topic as a whole phrase, not a substring: "fix" used to match
        # "prefix", and so almost anything.
        if _mentions(content, topic):
            return f"Skipped (topic already present): {topic[:60]}"

        # Insert under "## Active" (create if missing)
        match = active_re.search(content)
        if match:
            insert_pos = match.end()
            new_content = content[:insert_pos] + f"\n{final_line}" + content[insert_pos:]
        else:
            new_content = (
                content.rstrip() + f"\n\n## Active\n\n{final_line}\n"
                if content else f"# TODO\n\n## Active\n\n{final_line}\n"
            )
        write_atomic(todo_path, new_content)
    return f"Added to {project}/ai/todo.md [{kind}]: {topic[:60]}"


def handle_record_finding(arguments: Arguments) -> str:
    """Write {date}-{slug}.md under mirrors/{project}/findings/."""
    from datetime import datetime

    project = arguments.get("project", "").strip()
    title = one_line(arguments.get("title", ""))
    finding = arguments.get("finding", "").strip()
    why_it_matters = arguments.get("why_it_matters", "").strip()
    if not project or not title or not finding or not why_it_matters:
        raise ToolError("project, title, finding, and why_it_matters are required.")

    mirror = _project_mirror(project)
    if mirror is None:
        raise ToolError(
            f"project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    findings_dir = mirror / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    slug = _slugify(title)
    path = findings_dir / f"{date_str}-{slug}.md"

    tags = arguments.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    # One line each, and nothing that would end the frontmatter list early.
    tags = [t for t in (one_line(str(t).translate(_LIST_SYNTAX)) for t in tags) if t]
    source = one_line(arguments.get("source", ""))

    fm_lines = [
        "---",
        "type: finding",
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

    # A second finding with the same title on the same day gets -2, -3...
    # rather than replacing the first.
    from smriti.core.files import create_new

    path = create_new(path, fm + body)
    slug = path.stem.removeprefix(f"{date_str}-")

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
            try:
                with stub.open("x", encoding="utf-8") as f:
                    f.write(
                        f"---\n"
                        f"type: cross-project-finding-link\n"
                        f"tag: {tag}\n"
                        f"project: {project}\n"
                        f"date: {date_str}\n"
                        f"---\n\n"
                        f"# {title}\n\n"
                        f"Source: [[{rel_link}]]\n\n"
                        f"## Finding\n\n{finding}\n\n"
                        f"## Why it matters\n\n{why_it_matters}\n"
                    )
            except FileExistsError:
                continue
            cascade_msgs.append(tag_slug)

        # Update cross-project index (append-only, dedup by line content)
        from smriti.core.files import edit_lock, write_atomic

        idx_path = cp_root / "index.md"
        idx_line = f"- [[semantic/concepts/cross-project/findings/{_slugify(tags[0])}]] -- {title} ({project}, {date_str})"
        with edit_lock(idx_path):
            if idx_path.exists():
                idx_text = idx_path.read_text(encoding="utf-8")
            else:
                idx_text = (
                    "# Cross-Project Findings\n\n"
                    "Findings that touched multiple projects, indexed by tag. "
                    "Maintained by smriti_record_finding -- each tag dir collects "
                    "links to per-project findings.\n\n"
                )
            if idx_line not in idx_text:
                write_atomic(idx_path, idx_text.rstrip() + "\n" + idx_line + "\n")

    msg = f"Recorded finding: {project}/findings/{path.name}"
    if cascade_msgs:
        msg += f" (cross-project: {', '.join(cascade_msgs)})"
    return msg


def handle_record_decision(arguments: Arguments) -> str:
    """Write {date}-{slug}.md under mirrors/{project}/decisions/."""
    from datetime import datetime

    project = arguments.get("project", "").strip()
    title = one_line(arguments.get("title", ""))
    decision = arguments.get("decision", "").strip()
    rationale = arguments.get("rationale", "").strip()
    alternatives = arguments.get("alternatives_considered", "").strip()
    if not project or not title or not decision or not rationale:
        raise ToolError("project, title, decision, and rationale are required.")

    mirror = _project_mirror(project)
    if mirror is None:
        raise ToolError(
            f"project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    decisions_dir = mirror / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    slug = _slugify(title)
    path = decisions_dir / f"{date_str}-{slug}.md"
    source = one_line(arguments.get("source", ""))

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

    from smriti.core.files import create_new

    path = create_new(path, fm + body)  # same title, same day: -2, not a replacement
    return f"Recorded decision: {project}/decisions/{path.name}"


def handle_record_feature(arguments: Arguments) -> str:
    """Open a feature spec at {project}/.ai/features/active/{slug}.md."""
    from datetime import datetime

    project = arguments.get("project", "").strip()
    slug = arguments.get("slug", "").strip()
    title = one_line(arguments.get("title", ""))
    goal = arguments.get("goal", "").strip()
    plan = arguments.get("plan", "").strip()
    if not all([project, slug, title, goal, plan]):
        raise ToolError("project, slug, title, goal, and plan are required.")

    mirror = _project_mirror(project)
    if mirror is None:
        raise ToolError(
            f"project {project!r} has no mirror. "
            f"Known: {', '.join(_known_projects()) or '(none)'}."
        )

    from smriti.core.files import edit_lock

    # Features live in {project}/.ai/features/active/ (junctioned in via mirror/ai)
    base = mirror / "ai" / "features"
    features_active = base / "active"
    features_active.mkdir(parents=True, exist_ok=True)
    path = features_active / f"{slug}.md"
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    why_now = arguments.get("why_now", "").strip()
    constraints = arguments.get("constraints", "").strip()
    owner = arguments.get("owner", "both").strip().lower()
    if owner not in _feature_owners():
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

    # Refuse if the slug already exists in any of active/shipped/abandoned --
    # caller should use a different slug rather than overwrite. The features
    # lock keeps a concurrent open or move from slipping in between.
    with edit_lock(base):
        for sub in ("active", "shipped", "abandoned"):
            if (base / sub / f"{slug}.md").exists():
                raise ToolError(f"feature slug {slug!r} already exists at {sub}/{slug}.md")
        with path.open("x", encoding="utf-8") as f:
            f.write(fm + "\n".join(body))
    return f"Opened feature: {project}/.ai/features/active/{slug}.md"


def handle_update_feature_status(arguments: Arguments) -> str:
    """Move a feature between active/shipped/abandoned and update frontmatter."""
    from datetime import datetime
    import re as _re

    project = arguments.get("project", "").strip()
    slug = arguments.get("slug", "").strip()
    new_status = arguments.get("new_status", "").strip().lower()
    notes = arguments.get("notes", "").strip()

    valid_statuses = {"building", "reviewing", "shipped", "abandoned"}
    if new_status not in valid_statuses:
        raise ToolError(f"new_status must be one of {sorted(valid_statuses)}")

    mirror = _project_mirror(project)
    if mirror is None:
        raise ToolError(f"project {project!r} has no mirror.")

    from smriti.core.files import edit_lock, write_atomic

    base = mirror / "ai" / "features"
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

    # Under the features lock, so a concurrent update or open can't move the
    # spec, or lose an edit, between this read and the write.
    with edit_lock(base):
        # Find current location
        current_path = None
        for sub in ("active", "shipped", "abandoned"):
            candidate = base / sub / f"{slug}.md"
            if candidate.exists():
                current_path = candidate
                current_sub = sub
                break
        if current_path is None:
            raise ToolError(f"feature {slug!r} not found in {project}/.ai/features/")

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
        write_atomic(dest_path, text)
        if dest_path != current_path:
            current_path.unlink()

    if new_status == dest_sub or current_sub == dest_sub:
        return f"Updated feature {slug} -> status={new_status} (in {dest_sub}/)"
    return f"Moved feature {slug}: {current_sub}/ -> {dest_sub}/ (status={new_status})"


TOOLS = (
    Tool(
        name="smriti_add_todo",
        description="Append a new task line to a project's .ai/todo.md (via the mirror "
            "junction at ~/.narada/mirrors/{project}/ai/todo.md). Fails loud if "
            "the project slug is not a known mirror — no silent misroute. "
            "Substring-dedups on `topic` to avoid pile-up. Use Edit (not this "
            "tool) when restructuring or placing among related items — this tool "
            "is for append-only autonomous flows.",
        input_schema={
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
        handler=handle_add_todo,
    ),
    Tool(
        name="smriti_record_finding",
        description="Record a durable lesson learned for a project. Cascade-aware: "
            "writes to ~/.narada/mirrors/{project}/findings/{date}-{slug}.md, "
            "then upward propagation surfaces cross-project patterns at "
            "semantic/concepts/cross-project/findings/. Use whenever a "
            "session produces an insight worth carrying forward.",
        input_schema={
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
        handler=handle_record_finding,
    ),
    Tool(
        name="smriti_record_decision",
        description="Record an architectural decision (ADR) with alternatives considered. "
            "Writes to ~/.narada/mirrors/{project}/decisions/{date}-{slug}.md. "
            "Use for any choice you'd want to look up later as 'why did we do "
            "it that way?' -- protects against re-doing rejected paths.",
        input_schema={
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
        handler=handle_record_decision,
    ),
    Tool(
        name="smriti_record_feature",
        description="Open a new feature spec at {project}/.ai/features/active/{slug}.md "
            "(via the smriti mirror). The file is BOTH planning doc and "
            "(once shipped) the canonical documentation -- single source of "
            "truth, no drift. Status starts at 'planning'. Use "
            "smriti_update_feature_status to move through the lifecycle.",
        input_schema={
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
                    "enum": ["{user_key}", "{agent_key}", "both"],
                    "default": "both",
                },
            },
            "required": ["project", "slug", "title", "goal", "plan"],
        },
        handler=handle_record_feature,
    ),
    Tool(
        name="smriti_update_feature_status",
        description="Move a feature file between active/ -> shipped/ or abandoned/, "
            "updating frontmatter. Notes are appended as an Implementation "
            "Notes / Why Abandoned section depending on the destination.",
        input_schema={
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
        handler=handle_update_feature_status,
    ),
)

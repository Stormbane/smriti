"""Search-informed routing for new content entering the memory tree.

Routing is SEPARATE from cascade:
- Routing = initial placement (search-informed, one-shot JUDGE call)
- Cascade = transitive propagation (wikilink-following, recursive)

The routing JUDGE searches the tree for candidate pages, then decides
what action each candidate needs in ONE call. Actions:

- REVISE: candidate page needs rewriting with new info (EXECUTOR called)
- LINK:   candidate is related, add wikilink (no LLM)
- TASK:   relevant to a goal/project, append todo (no LLM)
- CREATE: no existing page fits, create a new concept page (EXECUTOR called)

Candidates not in the routing table are implicitly skipped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from smriti.store.judge import CallMetadata, _call_claude, executor_via_claude

log = logging.getLogger(__name__)


# ── Leaf filtering ──────────────────────────────────────────────────
# Time-stamped capture directories are LEAVES — immutable records that
# should not be routing targets. Override via NARADA_LEAF_PREFIXES
# (comma-separated path prefixes, forward slashes).

_DEFAULT_LEAF_PREFIXES = (
    "sources/",
    "heartbeat/artifacts/",
    "events/",
    "journal/",
    "days/",
    "episodes/",
    "notes/",
    "mirrors/",
    "inbox/",
)


def _load_leaf_prefixes() -> tuple[str, ...]:
    override = os.environ.get("NARADA_LEAF_PREFIXES")
    if override is None:
        return _DEFAULT_LEAF_PREFIXES
    prefixes = tuple(p.strip() for p in override.split(",") if p.strip())
    return prefixes or _DEFAULT_LEAF_PREFIXES


LEAF_PREFIXES = _load_leaf_prefixes()


def is_leaf_path(source: str) -> bool:
    """True if ``source`` lives under a leaf-capture directory."""
    normalized = source.replace("\\", "/").lstrip("./")
    return any(normalized.startswith(p) for p in LEAF_PREFIXES)


# ── Data types ──────────────────────────────────────────────────────


@dataclass
class RoutingAction:
    """A single routing decision for one candidate page."""

    action: str       # REVISE | LINK | TASK | CREATE
    target: str       # relative path (existing page or new path for CREATE)
    direction: str    # instructions for EXECUTOR / task text / link note
    reason: str


@dataclass
class RoutingResult:
    """The full routing table from one JUDGE call."""

    actions: list[RoutingAction] = field(default_factory=list)
    meta: CallMetadata = field(default_factory=CallMetadata)


# ── Action executors (no LLM) ──────────────────────────────────────


def execute_link(
    summary_path: Path,
    target_path: Path,
    root: Path,
) -> bool:
    """Add a wikilink between summary and target. Returns True if changed."""
    target_rel = str(target_path.relative_to(root).with_suffix("")).replace("\\", "/")
    link = f"[[{target_rel}]]"

    try:
        content = summary_path.read_text(encoding="utf-8")
    except OSError:
        log.warning("execute_link: cannot read %s", summary_path)
        return False

    # Already linked
    if link in content:
        return False

    # Find or create a Related section
    related_pat = re.compile(r"^##\s+(Related|See Also)\s*$", re.MULTILINE | re.IGNORECASE)
    match = related_pat.search(content)

    if match:
        # Insert after the heading
        insert_pos = match.end()
        content = content[:insert_pos] + f"\n- {link}" + content[insert_pos:]
    else:
        content = content.rstrip() + f"\n\n## Related\n\n- {link}\n"

    summary_path.write_text(content, encoding="utf-8")
    log.info("LINK: %s -> %s", summary_path.relative_to(root), target_rel)
    return True


def execute_task(
    target_path: Path,
    task_description: str,
    summary_path: Path,
    root: Path,
) -> bool:
    """Append a task to the target page's task/todo section. Returns True if changed."""
    summary_rel = str(summary_path.relative_to(root).with_suffix("")).replace("\\", "/")
    task_line = f"- [ ] {task_description} (from [[{summary_rel}]])"

    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError:
        log.warning("execute_task: cannot read %s", target_path)
        return False

    # Check for duplicate
    if task_description in content:
        return False

    # Find an existing tasks/todo section
    task_pat = re.compile(
        r"^##\s+(Tasks|TODO|Action Items|Next)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = task_pat.search(content)

    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + f"\n{task_line}" + content[insert_pos:]
    else:
        content = content.rstrip() + f"\n\n## Tasks\n\n{task_line}\n"

    target_path.write_text(content, encoding="utf-8")
    log.info("TASK: %s -> %s", task_description[:60], target_path.relative_to(root))
    return True


def execute_create(
    target_path: Path,
    direction: str,
    context: str,
    root: Path,
    executor_fn: Callable[..., str] = executor_via_claude,
) -> Path:
    """Create a new concept page via EXECUTOR. Returns the created path."""
    # If target already exists, this should have been downgraded to REVISE
    if target_path.exists():
        raise FileExistsError(f"Cannot CREATE: {target_path} already exists")

    prompt_direction = (
        f"Create a new knowledge page about: {direction}\n\n"
        f"Write clear, structured markdown with a heading. "
        f"Include wikilinks to related concepts where appropriate."
    )

    content = executor_fn(context, prompt_direction, context)

    # Ensure parent dirs exist
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Add frontmatter
    page = (
        f"---\ncreated_by: ingest\n---\n\n{content}\n"
    )
    target_path.write_text(page, encoding="utf-8")
    log.info("CREATE: %s", target_path.relative_to(root))
    return target_path


# ── Routing JUDGE ───────────────────────────────────────────────────

_DEFAULT_ROUTING_PROMPT = """\
You are routing new content into an existing knowledge tree.

Given the NEW CONTENT and a set of CANDIDATE PAGES, decide which candidates
need action. Return a JSON array of actions.

Each action object has these keys:
- "action": one of REVISE, LINK, TASK, CREATE
- "target": the candidate's relative path (for REVISE/LINK/TASK) or a new \
relative path (for CREATE)
- "direction": what to change (REVISE), the connection (LINK), the task \
description (TASK), or what the new page should cover (CREATE)
- "reason": brief rationale

Action types:
- REVISE: The candidate page needs rewriting to incorporate new information. \
Use when the new content materially changes or extends what the candidate says.
- LINK: The candidate is related but its content does not need changing. \
Add a cross-reference. Use when there is a topical connection but no \
information update.
- TASK: The new content is relevant to a goal or project page. Add a review \
task. Use for actionable items that a human should consider.
- CREATE: No existing candidate captures a concept that deserves its own page. \
Suggest a path under semantic/concepts/ and describe the topic. Use sparingly.

Candidates not in your response are implicitly skipped (no action needed).

Return ONLY a JSON array. If no actions are needed, return [].
"""


def routing_judge_auto_skip(
    content: str,
    candidates: list[dict],
    prompt_path: Path | None = None,
) -> RoutingResult:
    """Always returns empty routing table. For testing without LLM calls."""
    return RoutingResult()


def routing_judge_via_claude(
    content: str,
    candidates: list[dict],
    prompt_path: Path | None = None,
) -> RoutingResult:
    """Call ``claude -p`` with the routing JUDGE prompt.

    Parameters
    ----------
    content:
        The new content being ingested.
    candidates:
        List of dicts with keys: source, content, trunk_distance.
    prompt_path:
        Optional path to a custom prompt template.
    """
    if prompt_path and prompt_path.exists():
        template = prompt_path.read_text(encoding="utf-8")
    else:
        template = _DEFAULT_ROUTING_PROMPT

    # Build candidate listing
    candidate_lines = []
    for i, c in enumerate(candidates):
        candidate_lines.append(
            f"[{i + 1}] {c['source']} (trunk_distance: {c.get('trunk_distance', -1)})\n"
            f"{c['content'][:2000]}"
        )
    candidates_text = "\n\n---\n\n".join(candidate_lines) if candidate_lines else "(no candidates found)"

    prompt = (
        f"{template}\n\n"
        f"--- NEW CONTENT ---\n{content[:4000]}\n\n"
        f"--- CANDIDATE PAGES ({len(candidates)}) ---\n{candidates_text}\n\n"
        f"Respond with JSON array only."
    )

    raw, meta = _call_claude(prompt)

    # Parse JSON array from response
    actions = _parse_routing_response(raw)

    return RoutingResult(actions=actions, meta=meta)


def _parse_routing_response(raw: str) -> list[RoutingAction]:
    """Extract RoutingAction list from LLM response."""
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            actions = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                action = item.get("action", "").upper()
                if action not in ("REVISE", "LINK", "TASK", "CREATE"):
                    log.warning("Unknown routing action: %s", action)
                    continue
                actions.append(RoutingAction(
                    action=action,
                    target=item.get("target", ""),
                    direction=item.get("direction", ""),
                    reason=item.get("reason", ""),
                ))
            return actions
    except json.JSONDecodeError:
        pass

    log.warning("Could not parse routing response as JSON, returning empty actions")
    return []


# ── Main route function ────────────────────────────────────────────


def route(
    content: str,
    root: Path,
    *,
    top_k: int = 10,
    judge_fn: Callable[..., RoutingResult] | None = None,
    prompt_path: Path | None = None,
) -> RoutingResult:
    """Search the tree for candidates and route new content to them.

    Parameters
    ----------
    content:
        The new content to route.
    root:
        Tree root path.
    top_k:
        Number of search candidates to consider.
    judge_fn:
        Routing judge function. Defaults to ``routing_judge_via_claude``.
    prompt_path:
        Optional path to a custom routing prompt.

    Returns
    -------
    RoutingResult
        The routing table with actions for relevant candidates.
    """
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import ensure_schema
    from smriti.store.search import search

    if judge_fn is None:
        judge_fn = routing_judge_via_claude

    db_path = smriti_db_path()
    if not db_path.exists():
        log.warning("No search index found. Routing skipped. Run 'smriti index' first.")
        return RoutingResult()

    # Open index and search for candidates
    conn = ensure_schema(db_path, _get_dimension(db_path))
    try:
        results = search(conn, content[:1000], top_k=top_k, use_reranker=False)
    finally:
        conn.close()

    if not results:
        log.info("No search candidates found for routing.")
        return judge_fn(content, [], prompt_path)

    # Deduplicate by source path, filter out leaf captures, read full content
    seen_sources: set[str] = set()
    candidates: list[dict] = []
    leaves_skipped = 0
    for r in results:
        if r.source in seen_sources:
            continue
        seen_sources.add(r.source)

        if is_leaf_path(r.source):
            leaves_skipped += 1
            log.debug("Routing filter: skipping leaf candidate %s", r.source)
            continue

        # Read the full file for the candidate
        full_path = root / r.source
        try:
            full_content = full_path.read_text(encoding="utf-8")
        except OSError:
            full_content = r.content  # fall back to chunk
        candidates.append({
            "source": r.source,
            "content": full_content,
            "trunk_distance": r.trunk_distance,
        })

    if leaves_skipped:
        log.info("Routing: %d leaf candidates filtered out", leaves_skipped)

    return judge_fn(content, candidates, prompt_path)


def _get_dimension(db_path: Path) -> int:
    """Read embedding dimension from the index metadata."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
        return int(row[0]) if row else 384
    finally:
        conn.close()


# ── Shared action execution ────────────────────────────────────────


def execute_routing_actions(
    routing_result: RoutingResult,
    *,
    source_path: Path,
    root: Path,
    executor_fn: Callable[..., str] = executor_via_claude,
    context_content: str = "",
    dry_run: bool = False,
) -> dict:
    """Execute the actions in a routing table.

    Shared by ``ingest()`` (which summarizes first) and ``route_file()``
    (which routes an existing file directly).

    Returns a dict with ``actions_executed`` (list of per-action records)
    and ``cascade_targets`` (list of Paths that were REVISED/CREATEd).
    """
    from smriti.store.cascade import PROTECTED_FILES, queue_cognitive_cascade

    actions_executed: list[dict] = []
    cascade_targets: list[Path] = []

    for action in routing_result.actions:
        record = {
            "action": action.action,
            "target": action.target,
            "direction": action.direction[:100],
            "executed": False,
        }

        if dry_run:
            actions_executed.append(record)
            continue

        if action.action in ("REVISE", "CREATE") and is_leaf_path(action.target):
            log.warning(
                "Routing action %s on leaf path %s rejected (leaves are immutable)",
                action.action, action.target,
            )
            record["rejected_reason"] = "leaf-path"
            actions_executed.append(record)
            continue

        target_path = root / action.target

        if action.action == "REVISE":
            if target_path.name in PROTECTED_FILES:
                log.info("REVISE on protected file %s downgraded to PROMOTE", action.target)
                record["action"] = "PROMOTE"
                actions_executed.append(record)
                continue
            if not target_path.exists():
                log.warning("REVISE target not found: %s", action.target)
                actions_executed.append(record)
                continue
            parent_content = target_path.read_text(encoding="utf-8")
            revised = executor_fn(parent_content, action.direction, context_content)
            target_path.write_text(revised, encoding="utf-8")
            cascade_targets.append(target_path)
            record["executed"] = True
            log.info("REVISED: %s", action.target)

        elif action.action == "LINK":
            changed = execute_link(source_path, target_path, root)
            record["executed"] = changed

        elif action.action == "TASK":
            if not target_path.exists():
                log.warning("TASK target not found: %s", action.target)
                actions_executed.append(record)
                continue
            changed = execute_task(target_path, action.direction, source_path, root)
            record["executed"] = changed

        elif action.action == "CREATE":
            if target_path.exists():
                log.info("CREATE target exists, downgrading to REVISE: %s", action.target)
                parent_content = target_path.read_text(encoding="utf-8")
                revised = executor_fn(parent_content, action.direction, context_content)
                target_path.write_text(revised, encoding="utf-8")
                cascade_targets.append(target_path)
                record["action"] = "REVISE"
                record["executed"] = True
            else:
                created = execute_create(
                    target_path, action.direction, context_content, root, executor_fn,
                )
                cascade_targets.append(created)
                record["executed"] = True

        actions_executed.append(record)

    cascade_queued = 0
    if cascade_targets and not dry_run:
        cascade_queued = queue_cognitive_cascade(cascade_targets, root)
        log.info("Cascade queued: %d tasks", cascade_queued)

    return {
        "actions_executed": actions_executed,
        "cascade_targets": cascade_targets,
        "cascade_queued": cascade_queued,
    }


# ── Route file (no summarization) ──────────────────────────────────


def route_file(
    path: Path,
    root: Path,
    *,
    executor_fn: Callable[..., str] = executor_via_claude,
    routing_judge_fn: Callable[..., RoutingResult] | None = None,
    top_k: int = 10,
    dry_run: bool = False,
) -> dict:
    """Route an existing file: read content, search for candidates,
    JUDGE creates links. No summarization step.

    For files Narada writes directly — the content IS the content, no
    summary needed. Just discover what it connects to.

    Returns a dict with routing result + execution results.
    """
    if not path.exists():
        log.warning("route_file: path does not exist: %s", path)
        return {"actions_executed": [], "cascade_queued": 0}

    content = path.read_text(encoding="utf-8")

    routing_result = route(
        content,
        root,
        top_k=top_k,
        judge_fn=routing_judge_fn,
    )

    if not routing_result.actions:
        log.info("route_file: no actions for %s", path.relative_to(root))
        return {"routing": routing_result, "actions_executed": [], "cascade_queued": 0}

    exec_result = execute_routing_actions(
        routing_result,
        source_path=path,
        root=root,
        executor_fn=executor_fn,
        context_content=content,
        dry_run=dry_run,
    )

    return {
        "routing": routing_result,
        **exec_result,
    }

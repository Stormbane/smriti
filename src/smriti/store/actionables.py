"""Stage 4: actionables extraction.

After Stage 3 writes a threads synthesis page, this stage converts
synthesis into concrete work. Quality matters because downstream the
heartbeat reads these to pick what to build next.

Two implementations live in this module:

* ``extract_actionables_via_tools`` -- preferred. One claude -p invocation
  with the smriti_* MCP tools allowed. Claude reads the threads doc +
  project inventory and decides which tools to call (zero, one, or many
  per insight). Tools handle dedup, routing, and structure. Defer is
  first-class -- claude can simply not call a tool.

* ``extract_actionables`` -- legacy 3-stage prompt pipeline (insight ->
  scope -> concretize). Forces every insight into a fixed schema. Kept
  as fallback while the tool path beds in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from smriti.core.tree import tree_root
from smriti.store.judge import summarize_via_claude

log = logging.getLogger(__name__)


VALID_KINDS = {"research", "build", "implement", "fix", "goal_shift"}

# Tools the claude -p subprocess is allowed to call when extracting
# actionables. Smriti MCP server is registered in the user-level .mcp.json,
# so the subprocess inherits it.
_TOOL_ALLOW_LIST = ",".join([
    "mcp__smriti__smriti_ask_suti",
    "mcp__smriti__smriti_add_todo",
    "mcp__smriti__smriti_propose_goal_shift",
    "mcp__smriti__smriti_read",
])

_TOOL_PROMPT_TEMPLATE = """\
You are reading a "threads" page that synthesizes patterns across recent
concept pages. Your job is to convert what's actionable into work, using
the smriti_* tools. Defer is first-class: if an insight isn't worth
acting on yet, simply don't call a tool for it.

Tools available:

- `smriti_add_todo(project, topic, line, kind, source?)` -- append a task
  line to <project>/.ai/todo.md (via the smriti mirror). Kind must be one
  of research|build|implement|fix. Will fail loud if the project slug is
  unknown -- pick from the inventory below.
- `smriti_ask_suti(question, context, source?, urgency?)` -- queue a
  question for Suti when the right move depends on a decision he hasn't
  made. Use this INSTEAD OF fabricating a todo when there's real
  ambiguity about whether or how to act.
- `smriti_propose_goal_shift(title, rationale, proposed_change, source?)`
  -- only for shifts to existential goals (under ~/.narada/goals/). Rare.
- `smriti_read(query, top_k?)` -- search the memory tree for context
  before deciding. Use sparingly; threads doc + inventory should usually
  be enough.

Quality bar for todo `line`:

- Imperative, specific, buildable (NOT "investigate X" unless kind=research).
- Reference real files/modules/APIs from the project spec when relevant.
- Under 200 chars. If bigger, name the FIRST step only.
- Use the project's existing task-line format (check the inventory below).
- Source link via the `source` argument, not in the line itself.

Substring-dedup happens at the tool layer on `topic`, so you can be
permissive -- duplicates won't pile up.

--- PROJECT INVENTORY ---

{project_inventory}

--- THREADS PAGE ({threads_name}) ---

{threads_content}

When done, summarize in one sentence what you did. Do NOT list each
tool call -- the tools have their own success messages.
"""


# ── Tool-based extraction (preferred) ──────────────────────────────


@dataclass
class ToolActionablesResult:
    """Outcome of one tool-using extraction run."""
    todos_added_by_project: dict[str, int] = field(default_factory=dict)
    suti_comms_added: int = 0
    goal_proposals_added: int = 0
    elapsed_ms: int = 0
    summary_text: str = ""    # final assistant message
    error: str = ""

    @property
    def total_todos(self) -> int:
        return sum(self.todos_added_by_project.values())


def _snapshot_state(root: Path) -> dict:
    """Capture pre-call state of files the tools write to, so we can diff."""
    state: dict = {"todo_lines_by_project": {}}
    mirrors = root / "mirrors"
    if mirrors.exists():
        for sub in mirrors.iterdir():
            todo = sub / "ai" / "todo.md"
            if todo.exists():
                try:
                    state["todo_lines_by_project"][sub.name] = sum(
                        1 for _ in todo.open(encoding="utf-8")
                    )
                except OSError:
                    pass
    # outbox/ is the destination for suti-comms + goals-proposals (post-refactor).
    suti_comms = root / "outbox" / "suti-comms.md"
    state["suti_comms_count"] = (
        suti_comms.read_text(encoding="utf-8").count("\n## ")
        if suti_comms.exists() else 0
    )
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    proposals = root / "outbox" / f"goals-proposals-{date_str}.md"
    state["proposals_count"] = (
        proposals.read_text(encoding="utf-8").count("\n## ")
        if proposals.exists() else 0
    )
    return state


def _diff_state(before: dict, after: dict) -> ToolActionablesResult:
    """Compute additions per writer between two state snapshots."""
    result = ToolActionablesResult()
    for proj, n_after in after["todo_lines_by_project"].items():
        n_before = before["todo_lines_by_project"].get(proj, 0)
        delta = n_after - n_before
        if delta > 0:
            # Lines added != items added (multi-line items possible). Best-
            # effort: count items as the line delta. The tool's own success
            # messages are the source of truth for exact counts; this diff
            # is for reporting.
            result.todos_added_by_project[proj] = delta
    result.suti_comms_added = max(
        0, after["suti_comms_count"] - before["suti_comms_count"]
    )
    result.goal_proposals_added = max(
        0, after["proposals_count"] - before["proposals_count"]
    )
    return result


def _build_project_inventory(root: Path) -> str:
    """Build the project inventory block for the actionables prompt.

    Preferred source: ~/.narada/projects/projects.summary.md -- the
    canonical, hand-curated routing reference. When present, return it
    verbatim. Falls back to scanning mirrors when missing.
    """
    summary_path = root / "projects" / "projects.summary.md"
    if summary_path.exists():
        try:
            text = summary_path.read_text(encoding="utf-8")
            # Strip leading frontmatter for the prompt; keep the rest.
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end >= 0:
                    text = text[end + 5:].lstrip()
            return text.strip()
        except OSError:
            pass

    mirrors = root / "mirrors"
    if not mirrors.exists():
        return "(no projects.summary.md and no mirrors directory)"
    lines: list[str] = ["(fallback: projects.summary.md missing; scanning mirrors)"]
    for sub in sorted(mirrors.iterdir()):
        if not sub.is_dir():
            continue
        slug = sub.name
        todo = sub / "ai" / "todo.md"
        if not todo.exists():
            continue
        spec = sub / "knowledge" / "spec.md"
        summary = ""
        if spec.exists():
            try:
                m = re.search(r"^#\s+([^\n]+)", spec.read_text(encoding="utf-8")[:1500], re.MULTILINE)
                summary = m.group(1).strip() if m else slug
            except OSError:
                summary = slug
        lines.append(f"- `{slug}` -- {summary or slug}")
    return "\n".join(lines)


def extract_actionables_via_tools(
    threads_path: Path,
    root: Path,
    *,
    timeout: int | None = None,
) -> ToolActionablesResult:
    """One-shot tool-using extraction. Spawns claude -p with the smriti
    tools allowed; claude decides what to record."""
    t0 = time.monotonic()
    result = ToolActionablesResult()

    if not threads_path.exists():
        result.error = "threads path not found"
        return result
    try:
        threads_content = threads_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.error = f"read failed: {exc}"
        return result

    # Resolve claude binary the same way the rest of smriti does.
    claude = (
        os.environ.get("NARADA_CLAUDE_PATH")
        or shutil.which("claude")
    )
    if not claude:
        result.error = "claude CLI not found in PATH"
        return result

    if timeout is None:
        timeout = int(os.environ.get("NARADA_CLAUDE_TIMEOUT", "300"))

    threads_name = threads_path.name
    prompt = _TOOL_PROMPT_TEMPLATE.format(
        project_inventory=_build_project_inventory(root),
        threads_name=threads_name,
        threads_content=threads_content,
    )

    before = _snapshot_state(root)

    # --allowedTools must come BEFORE -p (positional ordering matters for
    # the claude CLI). Prompt is piped via stdin to dodge Windows command
    # line length limits.
    cmd = [
        claude,
        "--allowedTools", _TOOL_ALLOW_LIST,
        "-p",
        "--output-format", "text",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        result.error = f"claude -p timed out after {timeout}s"
        return result
    except Exception as exc:
        result.error = f"subprocess failed: {exc}"
        return result

    if proc.returncode != 0:
        result.error = (
            f"claude -p exit {proc.returncode}: {proc.stderr.strip()[:300]}"
        )
        return result

    summary_text = (proc.stdout or "").strip()
    after = _snapshot_state(root)
    diff_result = _diff_state(before, after)
    diff_result.summary_text = summary_text
    diff_result.elapsed_ms = int((time.monotonic() - t0) * 1000)

    log.info(
        "Actionables (tools) %s: %d todos across %d projects, %d suti-comms, "
        "%d goal proposals (%dms)",
        threads_name, diff_result.total_todos,
        len(diff_result.todos_added_by_project),
        diff_result.suti_comms_added, diff_result.goal_proposals_added,
        diff_result.elapsed_ms,
    )
    return diff_result


# ── Data types ─────────────────────────────────────────────────────


@dataclass
class Insight:
    """Stage A output: coarse 'could this be acted on?'"""
    topic: str
    kind: str
    rationale: str


@dataclass
class ScopedInsight:
    """Stage B output: insight + project binding."""
    topic: str
    kind: str
    rationale: str
    project: str                  # slug, or "" if unclaimed
    novel: bool                   # True if not already in target project's todo
    skip_reason: str = ""         # set if B rejected (duplicate / not applicable)


@dataclass
class Actionable:
    """Stage C output: concrete next-step."""
    topic: str
    kind: str
    project: str
    rationale: str
    todo_line: str                # exact markdown line, ready to append


@dataclass
class ActionablesResult:
    insights: list[Insight] = field(default_factory=list)
    scoped: list[ScopedInsight] = field(default_factory=list)
    actionables: list[Actionable] = field(default_factory=list)
    tasks_routed: int = 0
    global_tasks_routed: int = 0
    proposals_written: int = 0
    proposals_path: Path | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    error: str = ""


# ── Project inventory ──────────────────────────────────────────────


def _discover_projects(root: Path) -> dict[str, dict]:
    """Return {slug: {todo_path, spec_path, summary}} for each mirror.

    A project exists if ~/.narada/mirrors/{slug}/ exists. Its todo lives at
    mirrors/{slug}/ai/todo.md (junction to real project .ai/todo.md).
    Spec at mirrors/{slug}/knowledge/spec.md if present.
    """
    mirrors_dir = root / "mirrors"
    if not mirrors_dir.exists():
        return {}

    out: dict[str, dict] = {}
    for sub in sorted(mirrors_dir.iterdir()):
        if not sub.is_dir():
            continue
        slug = sub.name
        todo_path = sub / "ai" / "todo.md"
        spec_path = sub / "knowledge" / "spec.md"
        summary = ""
        if spec_path.exists():
            try:
                # First heading + first meaningful paragraph
                text = spec_path.read_text(encoding="utf-8")[:2000]
                match = re.search(r"^#\s+([^\n]+)", text, re.MULTILINE)
                summary = match.group(1).strip() if match else slug
            except OSError:
                summary = slug
        else:
            summary = slug
        out[slug] = {
            "todo_path": todo_path if todo_path.exists() else None,
            "spec_path": spec_path if spec_path.exists() else None,
            "summary": summary,
        }
    return out


def _load_project_todo(project_meta: dict) -> str:
    """Load the project's todo content, empty string if missing."""
    p = project_meta.get("todo_path")
    if p and p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


def _load_project_spec(project_meta: dict) -> str:
    """Load first 3000 chars of project spec for context."""
    p = project_meta.get("spec_path")
    if p and p.exists():
        try:
            return p.read_text(encoding="utf-8")[:3000]
        except OSError:
            return ""
    return ""


# ── Stage A: insight extraction ────────────────────────────────────


_STAGE_A_PROMPT = """\
You are reading a "threads" page that synthesizes patterns across a
batch of concept pages. Your job is the FIRST of three passes: extract
the coarse list of possible actions implied by the synthesis.

Do not propose specifics yet. That comes later. Here, name each possible
direction with:

- "topic": 2-6 word noun phrase naming the direction (e.g. "ESP32 TTS pipeline",
  "smriti queue rebuild", "merge-candidate detection")
- "kind": one of:
    "research"    -- investigate something before building
    "build"       -- new feature/component
    "implement"   -- fill in a planned-but-unimplemented thing
    "fix"         -- defect or gap
    "goal_shift"  -- suggested change to an existential goal (rare; human review)
- "rationale": one sentence tying the action to what in the threads page
  implies it.

Be generous at this stage: better to surface too many than too few.
Narrower filtering happens in later passes. If genuinely nothing is
implied, return []. Do not manufacture insights.

Return ONLY a JSON array of objects with keys: topic, kind, rationale.

--- THREADS PAGE ({threads_name}) ---

{threads_content}
"""


def _run_stage_a(
    threads_path: Path,
    threads_content: str,
    executor_fn: Callable[[str], tuple[str, object]],
) -> tuple[list[Insight], object]:
    """Stage A: coarse insight extraction."""
    prompt = _STAGE_A_PROMPT.format(
        threads_name=threads_path.name,
        threads_content=threads_content,
    )
    try:
        raw, meta = executor_fn(prompt)
    except Exception as exc:
        log.warning("Stage A failed: %s", exc)
        return [], None

    data = _parse_json_array(raw)
    insights: list[Insight] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).lower()
        if kind not in VALID_KINDS:
            continue
        topic = str(item.get("topic", "")).strip()
        if not topic:
            continue
        insights.append(Insight(
            topic=topic,
            kind=kind,
            rationale=str(item.get("rationale", "")).strip(),
        ))
    return insights, meta


# ── Stage B: project scoping ───────────────────────────────────────


_STAGE_B_PROMPT = """\
You are routing a list of coarse insights to specific projects. Each
insight has a topic, kind, and rationale. Your job is to decide for each:

1. Which project does this belong to? Pick a slug from the project
   inventory below, or set project="" if it doesn't fit any existing
   project.
2. Is this NOVEL, or is it already covered in that project's existing
   TODO list? Check the per-project TODO excerpts and set novel=false
   if the same work is already planned.
3. If the insight is not worth acting on (vague, duplicate, irrelevant),
   set skip_reason to a short explanation.

Do not restate kinds. Do not propose new tasks -- just scope existing
insights. A goal_shift kind always has project="" (goal shifts are
cross-project).

Return a JSON array matching the input length, with each object:
- "topic": echo from input
- "project": slug from inventory, or ""
- "novel": true | false
- "skip_reason": "" if acting, short string if skipping

--- PROJECTS ---

{project_summary}

--- PER-PROJECT TODO EXCERPTS ---

{todo_excerpts}

--- INSIGHTS TO SCOPE ---

{insights_json}
"""


def _run_stage_b(
    insights: list[Insight],
    projects: dict[str, dict],
    executor_fn: Callable[[str], tuple[str, object]],
) -> tuple[list[ScopedInsight], object]:
    """Stage B: project scoping, novelty check."""
    if not insights:
        return [], None

    project_summary = "\n".join(
        f"- `{slug}`: {meta['summary']}"
        for slug, meta in projects.items()
    ) or "(no projects discovered)"

    # Build TODO excerpts (capped)
    excerpt_parts: list[str] = []
    for slug, meta in projects.items():
        todo = _load_project_todo(meta)
        if not todo:
            continue
        excerpt = todo[:1500]
        excerpt_parts.append(f"### `{slug}` todo\n\n{excerpt}")
    todo_excerpts = "\n\n---\n\n".join(excerpt_parts) or "(no project todos found)"

    insights_json = json.dumps(
        [{"topic": i.topic, "kind": i.kind, "rationale": i.rationale} for i in insights],
        indent=2,
    )

    prompt = _STAGE_B_PROMPT.format(
        project_summary=project_summary,
        todo_excerpts=todo_excerpts,
        insights_json=insights_json,
    )

    try:
        raw, meta = executor_fn(prompt)
    except Exception as exc:
        log.warning("Stage B failed: %s", exc)
        # Degrade gracefully: no project, assume novel
        return (
            [ScopedInsight(topic=i.topic, kind=i.kind, rationale=i.rationale,
                           project="", novel=True) for i in insights],
            None,
        )

    data = _parse_json_array(raw)
    decisions_by_topic: dict[str, dict] = {}
    for item in data:
        if isinstance(item, dict) and item.get("topic"):
            decisions_by_topic[str(item["topic"]).strip()] = item

    scoped: list[ScopedInsight] = []
    for insight in insights:
        d = decisions_by_topic.get(insight.topic, {})
        scoped.append(ScopedInsight(
            topic=insight.topic,
            kind=insight.kind,
            rationale=insight.rationale,
            project=str(d.get("project", "")).strip() if insight.kind != "goal_shift" else "",
            novel=bool(d.get("novel", True)),
            skip_reason=str(d.get("skip_reason", "")).strip(),
        ))
    return scoped, meta


# ── Stage C: concretize ────────────────────────────────────────────


_STAGE_C_PROMPT = """\
Produce ONE concrete todo line for this scoped insight. The line will be
appended to a project's `.ai/todo.md` and read by an autonomous agent
(the heartbeat) to decide what to work on next.

Quality requirements:
- Imperative, specific, buildable. NOT "investigate X" unless kind=research;
  for build/implement/fix it should name a first concrete step.
- Reference specific files, modules, or APIs when the project spec and
  existing todo mention them.
- Under 200 chars. If the action is bigger, name the first step only.
- Use the project's established task-line format from the existing todo.
- Do NOT restate the rationale in the line itself. It can go inline as a
  brief parenthetical if helpful.

Return a JSON object with one key:
- "todo_line": the ready-to-append markdown line (no leading bullet if
  the project's format doesn't use them; include the bullet if it does)

--- PROJECT: {project} ---

{project_spec}

--- EXISTING TODO ---

{project_todo}

--- SCOPED INSIGHT ---

Kind: {kind}
Topic: {topic}
Rationale: {rationale}
"""


def _run_stage_c(
    insight: ScopedInsight,
    projects: dict[str, dict],
    executor_fn: Callable[[str], tuple[str, object]],
) -> tuple[Actionable | None, object]:
    """Stage C: concretize into a ready-to-append todo line."""
    meta = projects.get(insight.project, {})
    spec = _load_project_spec(meta) if insight.project else "(no project spec)"
    todo = _load_project_todo(meta) if insight.project else "(no project todo)"

    prompt = _STAGE_C_PROMPT.format(
        project=insight.project or "(unclaimed)",
        project_spec=spec[:3000],
        project_todo=todo[:3000],
        kind=insight.kind,
        topic=insight.topic,
        rationale=insight.rationale,
    )

    try:
        raw, exec_meta = executor_fn(prompt)
    except Exception as exc:
        log.warning("Stage C failed for %s: %s", insight.topic, exc)
        return None, None

    # Accept either JSON or a bare line if JSON parsing fails.
    line = ""
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            line = str(data.get("todo_line", "")).strip()
    except json.JSONDecodeError:
        pass
    if not line:
        # Fall back to first non-empty line
        for l in raw.strip().splitlines():
            l = l.strip()
            if l and not l.startswith("{") and not l.startswith("}"):
                line = l
                break
    if not line:
        return None, exec_meta

    return (
        Actionable(
            topic=insight.topic,
            kind=insight.kind,
            project=insight.project,
            rationale=insight.rationale,
            todo_line=line,
        ),
        exec_meta,
    )


# ── JSON parsing helper ────────────────────────────────────────────


def _parse_json_array(raw: str) -> list:
    """Extract the first JSON array from a possibly-wrapped executor response."""
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start < 0 or end <= start:
            return []
        return json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        log.warning("JSON parse failed: %s", exc)
        return []


# ── Routing ─────────────────────────────────────────────────────────


def _append_to_file(target: Path, line: str, *, dedup_key: str = "") -> bool:
    """Append a line under '## Active' (or create section). Dedup by key if provided."""
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        content = ""

    if dedup_key and dedup_key in content:
        return False

    active_re = re.compile(r"^##\s+Active\s*$", re.MULTILINE)
    match = active_re.search(content)
    if match:
        insert_pos = match.end()
        new_content = content[:insert_pos] + f"\n{line}" + content[insert_pos:]
    else:
        new_content = content.rstrip() + f"\n\n## Active\n\n{line}\n" if content else f"# TODO\n\n## Active\n\n{line}\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content, encoding="utf-8")
    return True


def _append_proposal(proposals_path: Path, actionable: Actionable, source_rel: str) -> None:
    existing = ""
    if proposals_path.exists():
        try:
            existing = proposals_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if not existing:
        existing = (
            f"---\n"
            f"type: goals-proposals\n"
            f"date: {datetime.now().astimezone().strftime('%Y-%m-%d')}\n"
            f"---\n\n"
            f"# Goals Proposals\n\n"
            f"Proposed shifts to `goals/` raised by the consolidate pipeline.\n"
            f"NOT applied automatically. Suti triages.\n\n"
        )
    block = [
        f"\n## {actionable.topic}\n",
        f"Source: [[{source_rel}]]  ",
        f"Kind: {actionable.kind}\n",
        f"\n{actionable.rationale}\n",
        f"\nProposed action:",
        f"\n> {actionable.todo_line}\n",
    ]
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(existing + "\n".join(block), encoding="utf-8")


# ── Public entrypoint ──────────────────────────────────────────────


def extract_actionables(
    threads_path: Path,
    root: Path,
    *,
    executor_fn: Callable[[str], tuple[str, object]] = summarize_via_claude,
) -> ActionablesResult:
    """Three-stage pipeline: insight -> scope -> concretize -> route."""
    result = ActionablesResult()
    t0 = time.monotonic()

    if not threads_path.exists():
        result.error = "threads path not found"
        return result

    try:
        threads_content = threads_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.error = f"read failed: {exc}"
        return result

    # Stage A
    insights, meta_a = _run_stage_a(threads_path, threads_content, executor_fn)
    result.insights = insights
    _accumulate_meta(result, meta_a)
    if not insights:
        log.info("No insights extracted from %s", threads_path.name)
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result

    # Stage B
    projects = _discover_projects(root)
    log.info("Stage B: %d insights against %d known projects", len(insights), len(projects))
    scoped, meta_b = _run_stage_b(insights, projects, executor_fn)
    result.scoped = scoped
    _accumulate_meta(result, meta_b)

    # Stage C -- per scoped insight that passed filtering
    source_rel = (
        str(threads_path.relative_to(root).with_suffix("")).replace("\\", "/")
        if threads_path.is_relative_to(root) else threads_path.stem
    )
    actionables: list[Actionable] = []
    for s in scoped:
        if s.skip_reason:
            log.info("Stage C skip: %s (%s)", s.topic, s.skip_reason)
            continue
        if not s.novel:
            log.info("Stage C skip: %s (already in %s todo)", s.topic, s.project or "tasks.md")
            continue
        actionable, meta_c = _run_stage_c(s, projects, executor_fn)
        _accumulate_meta(result, meta_c)
        if actionable is not None:
            actionables.append(actionable)
    result.actionables = actionables

    # Routing
    for a in actionables:
        suffix = f" (from [[{source_rel}]])"
        if a.kind == "goal_shift":
            date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
            proposals_path = root / "outbox" / f"goals-proposals-{date_str}.md"
            _append_proposal(proposals_path, a, source_rel)
            result.proposals_path = proposals_path
            result.proposals_written += 1
            continue

        if a.project and a.project in projects:
            target = projects[a.project]["todo_path"]
            if target is None:
                log.warning("Project %s has no todo.md; falling back to global tasks", a.project)
                target = root / "open-threads" / "tasks" / "tasks.md"
                line = _format_global_task(a, suffix)
                if _append_to_file(target, line, dedup_key=a.topic):
                    result.global_tasks_routed += 1
                continue
            line = a.todo_line.rstrip()
            if suffix not in line:
                line = f"{line}{suffix}"
            if _append_to_file(target, line, dedup_key=a.topic):
                result.tasks_routed += 1
        else:
            target = root / "open-threads" / "tasks" / "tasks.md"
            line = _format_global_task(a, suffix)
            if _append_to_file(target, line, dedup_key=a.topic):
                result.global_tasks_routed += 1

    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "Actionables %s: %d insights, %d scoped, %d concretized, "
        "%d to project todos, %d to global tasks, %d goal proposals",
        threads_path.name, len(insights), len(scoped), len(actionables),
        result.tasks_routed, result.global_tasks_routed, result.proposals_written,
    )
    return result


def _format_global_task(a: Actionable, suffix: str) -> str:
    """Format an unclaimed actionable as a global tasks.md line."""
    tag = f"`{a.project}` " if a.project else ""
    line = a.todo_line.rstrip()
    # If the line already has a checkbox, preserve it; else add one.
    if not re.match(r"^-\s*\[", line):
        line = f"- [ ] {tag}{line}"
    return f"{line}{suffix}" if suffix not in line else line


def _accumulate_meta(result: ActionablesResult, meta) -> None:
    if meta is None:
        return
    result.tokens_in += getattr(meta, "tokens_in", 0)
    result.tokens_out += getattr(meta, "tokens_out", 0)
    result.cost_usd += getattr(meta, "cost_usd", 0.0)

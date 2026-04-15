"""Ingest external content into the smriti memory tree.

Pipeline:
1. Read source content (file or directory)
2. Summarize via EXECUTOR -> write to tree under sources/ branch
3. Route: search tree for candidates, JUDGE decides actions
4. Execute routing actions (REVISE, LINK, TASK, CREATE)
5. Queue cascade for any REVISED or CREATEd pages
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from smriti.store.judge import CallMetadata, executor_via_claude
from smriti.store.router import (
    RoutingAction,
    RoutingResult,
    execute_create,
    execute_link,
    execute_task,
    route,
    routing_judge_via_claude,
)

log = logging.getLogger(__name__)

# Maximum source content size to send to EXECUTOR for summarization
_MAX_SOURCE_BYTES = 50_000


@dataclass
class IngestResult:
    """Result of an ingest operation."""

    source: str
    source_type: str        # "file" | "directory"
    summary_path: Path | None = None
    routing: RoutingResult = field(default_factory=RoutingResult)
    actions_executed: list[dict] = field(default_factory=list)
    cascade_queued: int = 0
    elapsed_ms: int = 0


# ── Source reading ──────────────────────────────────────────────────


def _read_source(source: str) -> tuple[str, str]:
    """Read source content. Returns (content, source_type)."""
    path = Path(source)

    if path.is_file():
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > _MAX_SOURCE_BYTES:
            log.warning(
                "Source truncated: %s (%d bytes -> %d bytes)",
                source, len(content), _MAX_SOURCE_BYTES,
            )
            content = content[:_MAX_SOURCE_BYTES]
        return content, "file"

    if path.is_dir():
        parts = []
        total = 0
        extensions = {".md", ".txt", ".py", ".rs", ".go", ".js", ".ts", ".yml", ".yaml", ".toml", ".json"}
        for f in sorted(path.rglob("*")):
            if not f.is_file() or f.suffix not in extensions:
                continue
            if any(p.startswith(".") for p in f.relative_to(path).parts):
                continue  # skip hidden dirs
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            header = f"## {f.relative_to(path)}\n\n"
            parts.append(header + text)
            total += len(header) + len(text)
            if total > _MAX_SOURCE_BYTES:
                log.warning("Directory content truncated at %d bytes", total)
                break
        if not parts:
            raise ValueError(f"No readable files found in {source}")
        return "\n\n---\n\n".join(parts), "directory"

    raise FileNotFoundError(f"Source not found: {source}")


# ── Ingest pipeline ────────────────────────────────────────────────


def ingest(
    source: str,
    root: Path | None = None,
    *,
    branch: str = "sources",
    dry_run: bool = False,
    no_route: bool = False,
    route_top_k: int = 10,
    executor_fn: Callable[..., str] = executor_via_claude,
    routing_judge_fn: Callable[..., RoutingResult] | None = None,
) -> IngestResult:
    """Ingest external content into the memory tree.

    Parameters
    ----------
    source:
        File path or directory to ingest.
    root:
        Tree root (defaults to tree_root()).
    branch:
        Branch for the summary page (default: "sources").
    dry_run:
        If True, create summary and route but don't execute actions.
    no_route:
        If True, create summary but skip routing entirely.
    route_top_k:
        Number of search candidates for routing.
    executor_fn:
        EXECUTOR function for summarization and page creation.
    routing_judge_fn:
        Routing JUDGE function. Defaults to routing_judge_via_claude.
    """
    from smriti.core.tree import tree_root
    from smriti.metrics import get_logger
    from smriti.store.cascade import PROTECTED_FILES, queue_cognitive_cascade
    from smriti.store.writer import write_entry

    t0 = time.monotonic()
    metrics = get_logger()

    if root is None:
        root = tree_root()

    result = IngestResult(source=source, source_type="unknown")

    # Step 1: Read source
    content, source_type = _read_source(source)
    result.source_type = source_type
    log.info("Read source: %s (%s, %d chars)", source, source_type, len(content))

    # Step 2: Summarize via EXECUTOR and write to tree
    summary_direction = (
        "Summarize this source material as a knowledge page for a memory tree. "
        "Write clear, structured markdown with a heading that captures the topic. "
        "Preserve key details, names, numbers, and conclusions. "
        "Include wikilinks to related concepts where appropriate using [[concept-name]] syntax."
    )
    summary_content = executor_fn(content, summary_direction, content)

    # Derive a title from the source
    source_path = Path(source)
    title = source_path.stem.replace("-", " ").replace("_", " ").title()

    summary_path = write_entry(
        summary_content,
        branch=branch,
        title=title,
        source_hint=f"ingest:{source}",
        root=root,
        reindex=True,
    )
    result.summary_path = summary_path
    log.info("Summary written: %s", summary_path.relative_to(root))

    metrics.log(
        "ingest_summary",
        source=source,
        source_type=source_type,
        summary_path=str(summary_path.relative_to(root)),
        content_len=len(content),
    )

    # Step 3: Route (unless --no-route)
    if no_route:
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result

    routing_result = route(
        summary_content,
        root,
        top_k=route_top_k,
        judge_fn=routing_judge_fn,
    )
    result.routing = routing_result

    if not routing_result.actions:
        log.info("Routing: no actions needed.")
        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result

    # Step 4: Execute routing actions
    from smriti.store.router import is_leaf_path

    cascade_targets: list[Path] = []

    for action in routing_result.actions:
        action_record = {
            "action": action.action,
            "target": action.target,
            "direction": action.direction[:100],
            "executed": False,
        }

        if dry_run:
            action_record["executed"] = False
            result.actions_executed.append(action_record)
            continue

        # Belt-and-braces: reject REVISE/CREATE on leaf paths. LINK to a leaf
        # is allowed (cross-reference to a capture is fine); TASK to a leaf
        # is weird but not damaging.
        if action.action in ("REVISE", "CREATE") and is_leaf_path(action.target):
            log.warning(
                "Routing action %s on leaf path %s rejected (leaves are immutable)",
                action.action, action.target,
            )
            action_record["executed"] = False
            action_record["rejected_reason"] = "leaf-path"
            result.actions_executed.append(action_record)
            continue

        target_path = root / action.target

        if action.action == "REVISE":
            # Protected files get PROMOTE treatment
            if target_path.name in PROTECTED_FILES:
                log.info("REVISE on trunk file %s downgraded to PROMOTE", action.target)
                action_record["action"] = "PROMOTE"
                action_record["executed"] = False
                result.actions_executed.append(action_record)
                continue

            if not target_path.exists():
                log.warning("REVISE target not found: %s", action.target)
                result.actions_executed.append(action_record)
                continue

            # Call EXECUTOR to revise
            parent_content = target_path.read_text(encoding="utf-8")
            revised = executor_fn(parent_content, action.direction, summary_content)
            target_path.write_text(revised, encoding="utf-8")
            cascade_targets.append(target_path)
            action_record["executed"] = True
            log.info("REVISED: %s", action.target)

        elif action.action == "LINK":
            changed = execute_link(summary_path, target_path, root)
            action_record["executed"] = changed

        elif action.action == "TASK":
            if not target_path.exists():
                log.warning("TASK target not found: %s", action.target)
                result.actions_executed.append(action_record)
                continue
            changed = execute_task(target_path, action.direction, summary_path, root)
            action_record["executed"] = changed

        elif action.action == "CREATE":
            # If target already exists, downgrade to REVISE
            if target_path.exists():
                log.info("CREATE target exists, downgrading to REVISE: %s", action.target)
                parent_content = target_path.read_text(encoding="utf-8")
                revised = executor_fn(parent_content, action.direction, summary_content)
                target_path.write_text(revised, encoding="utf-8")
                cascade_targets.append(target_path)
                action_record["action"] = "REVISE"
                action_record["executed"] = True
            else:
                created = execute_create(
                    target_path, action.direction, summary_content, root, executor_fn,
                )
                cascade_targets.append(created)
                action_record["executed"] = True

        result.actions_executed.append(action_record)

    # Step 5: Queue cascade for REVISED/CREATEd pages
    if cascade_targets and not dry_run:
        result.cascade_queued = queue_cognitive_cascade(cascade_targets, root)
        log.info("Cascade queued: %d tasks", result.cascade_queued)

    result.elapsed_ms = int((time.monotonic() - t0) * 1000)

    metrics.log(
        "ingest_completed",
        source=source,
        source_type=source_type,
        summary_path=str(summary_path.relative_to(root)),
        actions_total=len(routing_result.actions),
        actions_executed=sum(1 for a in result.actions_executed if a.get("executed")),
        cascade_queued=result.cascade_queued,
        elapsed_ms=result.elapsed_ms,
        dry_run=dry_run,
    )

    return result

"""Cascade mechanism for the smriti memory tree.

Two modes:

**Structural cascade** (automatic, no LLM):
When a file is written, regenerate parent index.md files from directory
listings. This is housekeeping.

**Cognitive cascade** (queued, uses JUDGE→EXECUTOR loop):
After structural cascade, queue reviews of upstream references (threads,
goals, values). The JUDGE decides whether the parent needs updating; the
EXECUTOR generates revised content per the JUDGE's direction.

Cascade depth = significance. Most writes stop at structural level (depth 0).
A write that cascades to threads is notable (depth 1-2). A write that reaches
goals or values is rare and significant (depth 3+). A write that reaches
MEMORY.md is flagged for human review (trunk-level, never auto-applied).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from smriti.core.tree import tree_root
from smriti.store.judge import JudgmentResult, judge_auto_keep, executor_echo
from smriti.store.queue import QueueTask, enqueue

log = logging.getLogger(__name__)

# Files that require human review — cascade PROMOTEs, never REVISEs.
# The heartbeat and smriti's auto-cascade must not write to these.
# Override via the NARADA_PROTECTED_FILES env var (comma-separated filenames).
import os as _os

_DEFAULT_PROTECTED_FILES = frozenset({
    "MEMORY.md", "identity.md", "manifest.md", "mind.md", "suti.md",
})


def _load_protected_files() -> frozenset[str]:
    override = _os.environ.get("NARADA_PROTECTED_FILES")
    if override is None:
        return _DEFAULT_PROTECTED_FILES
    names = {n.strip() for n in override.split(",") if n.strip()}
    return frozenset(names) if names else _DEFAULT_PROTECTED_FILES


PROTECTED_FILES = _load_protected_files()

# Deprecated alias — use PROTECTED_FILES. Kept temporarily for compatibility.
TRUNK_FILES = PROTECTED_FILES

MAX_CASCADE_DEPTH = 5


# ── Structural cascade ──────────────────────────────────────────────


def _generate_index(directory: Path, root: Path) -> str:
    """Generate an index.md body from a directory's contents."""
    entries = sorted(directory.iterdir())
    lines = [f"# {directory.name.replace('-', ' ').title()}", ""]

    for entry in entries:
        if entry.name.startswith(".") or entry.name == "index.md":
            continue

        rel = str(entry.relative_to(root)).replace("\\", "/")
        name = entry.stem.replace("-", " ").replace("_", " ")

        if entry.is_dir():
            lines.append(f"- [[{rel}/]] — {name}")
        elif entry.suffix == ".md":
            rel_no_ext = str(entry.relative_to(root).with_suffix("")).replace("\\", "/")
            # Try to extract the first heading
            try:
                text = entry.read_text(encoding="utf-8")
                heading_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
                desc = heading_match.group(1) if heading_match else name
            except OSError:
                desc = name
            lines.append(f"- [[{rel_no_ext}]] — {desc}")

    lines.append("")
    return "\n".join(lines)


def structural_cascade(path: Path, root: Path | None = None) -> list[Path]:
    """Regenerate parent index.md files up the tree.

    Returns the list of index.md files that were updated.
    Does NOT touch MEMORY.md or trunk-level files.
    """
    import time

    t0 = time.monotonic()

    if root is None:
        root = tree_root()

    updated: list[Path] = []
    current = path.parent if path.is_file() else path

    while current != root and current.is_relative_to(root):
        index_file = current / "index.md"
        if index_file.exists():
            new_content = _generate_index(current, root)
            old_content = index_file.read_text(encoding="utf-8")

            if new_content.strip() != old_content.strip():
                index_file.write_text(new_content, encoding="utf-8")
                updated.append(index_file)
                log.info("Structural cascade: updated %s", index_file.relative_to(root))

        current = current.parent

    from smriti.metrics import get_logger
    get_logger().log("structural_cascade", trigger_path=str(path.relative_to(root)), indexes_updated=len(updated), elapsed_ms=int((time.monotonic() - t0) * 1000))
    return updated


# ── Wikilink reference finder ────────────────────────────────────────


def find_upstream_references(path: Path, root: Path) -> list[Path]:
    """Find files in the tree that wikilink to *path*.

    Searches for ``[[relative/path]]`` patterns. Returns paths of files
    that reference the given file.
    """
    rel = path.relative_to(root)
    # Normalize to forward slashes for wikilink matching (Windows compat)
    rel_fwd = str(rel).replace("\\", "/")
    rel_no_ext = str(rel.with_suffix("")).replace("\\", "/")
    # Build search patterns: with and without .md extension
    patterns = [
        f"[[{rel_no_ext}]]",
        f"[[{rel_fwd}]]",
        f"[[{rel.stem}]]",  # bare name
    ]

    refs: list[Path] = []
    for md_file in root.rglob("*.md"):
        if md_file == path or ".smriti" in md_file.parts:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
            for pat in patterns:
                if pat in content:
                    refs.append(md_file)
                    break
        except OSError:
            continue

    return refs


# ── Cognitive cascade ────────────────────────────────────────────────


def cognitive_cascade(
    changed_path: Path,
    root: Path | None = None,
    *,
    judge_fn: Callable[..., JudgmentResult] = judge_auto_keep,
    executor_fn: Callable[..., str] = executor_echo,
    judge_prompt: Path | None = None,
    executor_prompt: Path | None = None,
    depth: int = 0,
    dry_run: bool = False,
    visited: set[Path] | None = None,
) -> dict:
    """Run cognitive cascade from a changed file upward.

    Returns a dict with cascade stats: depth reached, verdicts, files changed.

    Cycle protection: ``visited`` tracks paths already processed in this
    cascade. A wikilink graph with cycles (A → B → C → A) will not re-revise
    files. Each path is cascaded from at most once per top-level invocation.
    """
    if root is None:
        root = tree_root()

    if visited is None:
        visited = set()

    stats = {"depth": depth, "verdicts": [], "files_changed": [], "promoted": [], "max_depth": depth}

    # Cycle protection — skip if we've already cascaded from this path
    resolved = changed_path.resolve()
    if resolved in visited:
        log.debug("Cascade cycle detected, skipping revisit of %s", changed_path)
        return stats
    visited.add(resolved)

    if depth >= MAX_CASCADE_DEPTH:
        log.warning("Cascade depth limit reached at %s", changed_path)
        return stats

    # Read the changed content
    try:
        child_content = changed_path.read_text(encoding="utf-8")
    except OSError:
        return stats

    # Find upstream references
    upstream = find_upstream_references(changed_path, root)
    if not upstream:
        log.debug("No upstream references for %s", changed_path.relative_to(root))
        return stats

    for parent_path in upstream:
        parent_rel = parent_path.relative_to(root)

        # Trunk-level files require human review
        if parent_path.name in PROTECTED_FILES:
            stats["promoted"].append(str(parent_rel))
            log.info(
                "Cascade reached trunk: %s (flagged for human review, not auto-applied)",
                parent_rel,
            )
            continue

        try:
            parent_content = parent_path.read_text(encoding="utf-8")
        except OSError:
            continue

        # JUDGE: does the parent need updating?
        judgment = judge_fn(parent_content, child_content, judge_prompt)
        from smriti.metrics import get_logger
        get_logger().log("cascade_verdict", parent=str(parent_rel), child=str(changed_path.relative_to(root)), verdict=judgment.verdict, direction_len=len(judgment.direction), reason=judgment.reason, judge_tokens_in=judgment.meta.tokens_in if hasattr(judgment, 'meta') else 0, judge_tokens_out=judgment.meta.tokens_out if hasattr(judgment, 'meta') else 0, judge_ms=judgment.meta.elapsed_ms if hasattr(judgment, 'meta') else 0)
        stats["verdicts"].append({
            "parent": str(parent_rel),
            "verdict": judgment.verdict,
            "reason": judgment.reason,
        })

        if judgment.verdict == "KEEP":
            log.debug("KEEP: %s (reason: %s)", parent_rel, judgment.reason)
            continue

        elif judgment.verdict == "REVISE":
            if dry_run:
                log.info("DRY RUN — would REVISE: %s (direction: %s)", parent_rel, judgment.direction)
                stats["files_changed"].append(str(parent_rel))
                continue

            # EXECUTOR: generate revised content
            revised = executor_fn(parent_content, judgment.direction, child_content, executor_prompt)

            # Write revised content
            parent_path.write_text(revised, encoding="utf-8")
            stats["files_changed"].append(str(parent_rel))
            log.info("REVISED: %s (reason: %s)", parent_rel, judgment.reason)

            # Recurse: cascade the parent's change upward
            sub_stats = cognitive_cascade(
                parent_path,
                root,
                judge_fn=judge_fn,
                executor_fn=executor_fn,
                judge_prompt=judge_prompt,
                executor_prompt=executor_prompt,
                depth=depth + 1,
                dry_run=dry_run,
                visited=visited,
            )
            stats["max_depth"] = max(stats["max_depth"], sub_stats["max_depth"])
            stats["verdicts"].extend(sub_stats["verdicts"])
            stats["files_changed"].extend(sub_stats["files_changed"])
            stats["promoted"].extend(sub_stats["promoted"])

        elif judgment.verdict == "REJECT":
            log.info("REJECTED: %s at %s (reason: %s)", changed_path.name, parent_rel, judgment.reason)

        elif judgment.verdict == "PROMOTE":
            stats["promoted"].append(str(parent_rel))
            log.info("PROMOTED: %s (flagged for upstream review)", parent_rel)

    return stats


# ── Queue integration ────────────────────────────────────────────────


def queue_cognitive_cascade(
    changed_paths: list[Path],
    root: Path | None = None,
    priority: int = 5,
) -> int:
    """Enqueue cognitive cascade tasks for the given changed files.

    Returns the number of tasks enqueued.
    """
    if root is None:
        root = tree_root()

    count = 0
    for p in changed_paths:
        upstream = find_upstream_references(p, root)
        if upstream:
            enqueue(
                QueueTask(
                    type="cognitive_cascade",
                    path=str(p.relative_to(root)),
                    parent=None,
                    priority=priority,
                ),
                root=root,
            )
            count += 1
    return count

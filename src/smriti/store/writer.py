"""Write new entries into the narada memory tree.

This is the WRITE step of the smriti pipeline — simplified for v0.1
(no JUDGE, no CROSSLINK). The full pipeline is:

    CAPTURE → EXTRACT → JUDGE → WRITE → CROSSLINK → INDEX

In v0.1 we go directly:

    write_entry() → file on disk → index_tree() on that one file

The JUDGE step is left as a stub. When the Qwen+LoRA viveka is stable,
JUDGE will sit between the caller and write_entry: the caller proposes,
the viveka disposes, and only approved content reaches the filesystem.

Entry format
------------
Each entry is a markdown file under <tree_root>/<branch>/YYYY/MM-DD-NNN.md.
The frontmatter records provenance so the cascade and index can track
where content came from.

Example layout:

    ~/.narada/
        journal/
            2026/
                04-13-001.md
                04-13-002.md
        notes/
            2026/
                04-13-001.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _next_counter(branch_date_dir: Path, date_str: str) -> int:
    """Return the next NNN counter for today's entries in branch_date_dir."""
    existing = sorted(branch_date_dir.glob(f"{date_str}-*.md"))
    # Filter to only files that match the MMDD-NNN pattern (not sub-directories)
    entries = [p for p in existing if p.is_file()]
    return len(entries) + 1


def write_entry(
    content: str,
    *,
    branch: str = "journal",
    title: str | None = None,
    source_hint: str | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> Path:
    """Write a new markdown entry to the narada tree.

    Parameters
    ----------
    content:
        The text to store.  Plain markdown; frontmatter will be prepended.
    branch:
        Subdirectory under the tree root.  Defaults to ``journal``.
        Common values: ``journal``, ``notes``, ``observations``.
    title:
        Optional heading for the entry.  If omitted, a datestamp is used.
    source_hint:
        Optional provenance label (e.g. ``"heartbeat"``, ``"manual"``,
        ``"precompact"``, a session UUID).  Stored in frontmatter.
    root:
        Tree root (defaults to ``tree_root()``).
    reindex:
        If True (default), run an incremental index after writing so the
        new entry is immediately searchable.

    Returns
    -------
    Path
        Absolute path of the written file.
    """
    from smriti.core.tree import tree_root

    if root is None:
        root = tree_root()

    now = datetime.now(timezone.utc)
    year_str = now.strftime("%Y")
    mmdd_str = now.strftime("%m-%d")

    # Directory: <root>/<branch>/<YYYY>/
    branch_year_dir = root / branch / year_str
    branch_year_dir.mkdir(parents=True, exist_ok=True)

    counter = _next_counter(branch_year_dir, mmdd_str)
    filename = f"{mmdd_str}-{counter:03d}.md"
    entry_path = branch_year_dir / filename

    heading = title or f"Entry {now.strftime('%Y-%m-%d %H:%M UTC')}"

    # Build frontmatter + content
    frontmatter_lines = [
        "---",
        f"date: {now.strftime('%Y-%m-%d')}",
        f"time: {now.strftime('%H:%M:%S')} UTC",
        f"branch: {branch}",
    ]
    if source_hint:
        frontmatter_lines.append(f"source: {source_hint}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(f"# {heading}")
    frontmatter_lines.append("")

    body = "\n".join(frontmatter_lines) + content.strip() + "\n"
    entry_path.write_text(body, encoding="utf-8")
    log.info("Wrote entry: %s", entry_path)
    from smriti.metrics import get_logger
    get_logger().log("write_entry", path=str(entry_path.relative_to(root)), branch=branch, content_bytes=len(content), has_title=title is not None, source_hint=source_hint or "")

    if reindex:
        _reindex_one(entry_path, root)

    # Structural cascade: update parent index.md files
    _structural_cascade(entry_path, root)

    return entry_path


def _reindex_one(path: Path, root: Path) -> None:
    """Run an incremental index — will pick up the new file by mtime."""
    try:
        from smriti.store.indexer import index_tree

        stats = index_tree(root=root)
        log.info(
            "Reindex after write: scanned=%d indexed=%d chunks=%d",
            stats["scanned"],
            stats["indexed"],
            stats["chunks"],
        )
    except Exception as exc:
        # Non-fatal: entry is on disk even if indexing failed.
        log.warning("Reindex after write failed: %s", exc)


def _structural_cascade(path: Path, root: Path) -> None:
    """Run structural cascade and queue cognitive cascade tasks."""
    try:
        from smriti.store.cascade import queue_cognitive_cascade, structural_cascade

        updated = structural_cascade(path, root)
        if updated:
            log.info("Structural cascade updated %d index files", len(updated))
            # Queue cognitive cascade for upstream review
            all_changed = [path] + updated
            queued = queue_cognitive_cascade(all_changed, root)
            if queued:
                log.info("Queued %d cognitive cascade tasks", queued)
    except Exception as exc:
        # Non-fatal: the write succeeded, cascade is bonus.
        log.warning("Structural cascade failed: %s", exc)

"""Write new entries into the narada memory tree.

This is the WRITE step of the smriti pipeline -- simplified for v0.1
(no JUDGE, no CROSSLINK). The full pipeline is:

    CAPTURE -> EXTRACT -> JUDGE -> WRITE -> CROSSLINK -> INDEX

In v0.1 we go directly:

    write_entry() -> file on disk -> index_tree() on that one file

Entry format
------------
Journal entries use a cascading time structure:

    journal/YYYY/MM/weekN/MM-DD.md

where weekN is the week within the month (week1=days 1-7, week2=8-14,
week3=15-21, week4=22-28, week5=29-31). Summary files at each level
(weekN.md, MM.md, YYYY.md) are created by the journal_rollup sleep
task and updated by cognitive cascade.

Non-journal branches use the simpler flat structure:

    <branch>/YYYY/MM-DD.md

Multiple writes on the same day append to the same daily file,
separated by horizontal rules. Concurrency-safe via O_CREAT|O_EXCL
for creation and O_APPEND for appends.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _week_of_month(day: int) -> int:
    """Return the week number within a month (1-5).

    week1 = days 1-7, week2 = 8-14, week3 = 15-21,
    week4 = 22-28, week5 = 29-31.
    """
    return (day - 1) // 7 + 1


def _journal_path(root: Path, branch: str, now: datetime) -> Path:
    """Compute the daily file path for a journal entry.

    Returns: <root>/journal/YYYY/MM/weekN/MM-DD.md
    """
    year_str = now.strftime("%Y")
    month_str = now.strftime("%m")
    mmdd_str = now.strftime("%m-%d")
    week = _week_of_month(now.day)

    entry_dir = root / branch / year_str / month_str / f"week{week}"
    entry_dir.mkdir(parents=True, exist_ok=True)
    return entry_dir / f"{mmdd_str}.md"


def _flat_path(root: Path, branch: str, now: datetime) -> Path:
    """Compute the daily file path for a non-journal entry.

    Returns: <root>/<branch>/YYYY/MM-DD.md
    """
    year_str = now.strftime("%Y")
    mmdd_str = now.strftime("%m-%d")

    entry_dir = root / branch / year_str
    entry_dir.mkdir(parents=True, exist_ok=True)
    return entry_dir / f"{mmdd_str}.md"


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
        Common values: ``journal``, ``notes``, ``projects/foo``.
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

    # Use local time for file paths and dates (journal entries are human-facing).
    # Store UTC in frontmatter for precision.
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()

    # Journal uses cascading time structure; other branches use flat
    if branch == "journal" or branch.startswith("journal/"):
        entry_path = _journal_path(root, branch, now_local)
    else:
        entry_path = _flat_path(root, branch, now_local)

    heading = title or f"Entry {now_local.strftime('%Y-%m-%d %H:%M')}"

    # Build the entry block
    entry_lines = [
        "---",
        f"date: {now_local.strftime('%Y-%m-%d')}",
        f"time: {now_utc.strftime('%H:%M:%S')} UTC",
        f"branch: {branch}",
    ]
    if source_hint:
        entry_lines.append(f"source: {source_hint}")
    entry_lines.append("---")
    entry_lines.append("")
    entry_lines.append(f"# {heading}")
    entry_lines.append("")

    entry_block = "\n".join(entry_lines) + content.strip() + "\n"

    # Write: create or append
    _append_entry(entry_path, entry_block)

    log.info("Wrote entry: %s", entry_path)
    from smriti.metrics import get_logger
    get_logger().log(
        "write_entry",
        path=str(entry_path.relative_to(root)),
        branch=branch,
        content_bytes=len(content),
        has_title=title is not None,
        source_hint=source_hint or "",
    )

    if reindex:
        _reindex_one(entry_path, root)

    # Structural cascade: update parent index.md files (sync; also enqueues
    # cognitive_cascade for any updated index files).
    _structural_cascade(entry_path, root)

    # Route through the central classifier for all async pipelines
    # (summarize, ingest/route, journal_rollup, wake_summary). Reindex
    # tasks from the classifier are skipped because we ran it synchronously
    # above.
    try:
        from smriti.store.queue import enqueue
        from smriti.store.watch_router import classify_write

        tasks = classify_write(entry_path, root, event_type="created")
        for task in tasks:
            if task.type == "reindex":
                continue
            enqueue(task, root=root)
    except Exception as exc:
        log.warning("classify_write after write failed: %s", exc)

    return entry_path


def _append_entry(path: Path, entry_block: str) -> None:
    """Append an entry to a daily file, creating it atomically if needed."""
    data = entry_block.encode("utf-8")

    if not path.exists():
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            return
        except FileExistsError:
            pass

    separator = b"\n---\n\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, separator + data)
    finally:
        os.close(fd)


def _reindex_one(path: Path, root: Path) -> None:
    """Index just the written file — the write path's fast lane.

    A full ``index_tree`` here meant every MCP server did a ~7000-file
    scan per write while contending for the one write lock (the
    "connection closed" era). ``index_file`` embeds only this entry's
    chunks in one short transaction; the nightly full pass remains the
    backstop for anything a degraded write leaves behind.
    """
    try:
        from smriti.store.indexer import index_file

        chunks = index_file(path, root=root)
        log.info("Indexed written file %s (%d chunks)", path.name, chunks)
    except Exception as exc:
        log.warning("Reindex after write failed: %s", exc)


def _structural_cascade(path: Path, root: Path) -> None:
    """Run structural cascade and queue cognitive cascade tasks."""
    try:
        from smriti.store.cascade import queue_cognitive_cascade, structural_cascade

        updated = structural_cascade(path, root)
        if updated:
            log.info("Structural cascade updated %d index files", len(updated))
            all_changed = [path] + updated
            queued = queue_cognitive_cascade(all_changed, root)
            if queued:
                log.info("Queued %d cognitive cascade tasks", queued)
    except Exception as exc:
        log.warning("Structural cascade failed: %s", exc)

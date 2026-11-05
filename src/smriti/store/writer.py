"""Write new entries into the memory tree.

``write_entry`` appends the entry to its daily file, indexes that one
file, and refreshes the ``index.md`` listings of its parent directories.

Entry format
------------
Journal entries use a cascading time structure:

    journal/YYYY/MM/weekN/MM-DD.md

where weekN is the week within the month (week1=days 1-7, week2=8-14,
week3=15-21, week4=22-28, week5=29-31).

Non-journal branches use the simpler flat structure:

    <branch>/YYYY/MM-DD.md

Multiple writes on the same day append to the same daily file,
separated by horizontal rules, under the file's edit lock.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from smriti.core.frontmatter import one_line

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


def _checked_branch(root: Path, branch: str) -> str:
    """Return *branch* normalised, or raise if it would leave *root*.

    ``branch`` reaches here straight from the MCP ``smriti_write`` tool, so
    ``..`` segments, absolute or drive paths, and paths that resolve outside
    the tree through a junction or symlink (e.g. ``mirrors/``) are rejected.
    """
    raw = branch.replace("\\", "/").strip()
    norm = raw.rstrip("/")
    parts = norm.split("/")
    if (not norm or raw.startswith("/") or ":" in norm or norm.startswith("~")
            or any(p in ("", ".", "..") for p in parts)):
        raise ValueError(f"invalid branch {branch!r}: use a relative path inside the memory tree")
    base = root.resolve()
    if not (base / norm).resolve().is_relative_to(base):
        raise ValueError(f"invalid branch {branch!r}: resolves outside the memory tree")
    return norm


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
    branch = _checked_branch(root, branch)

    # Use local time for file paths and dates (journal entries are human-facing).
    # Store UTC in frontmatter for precision.
    from smriti.daylog.model import day_tz

    now_utc = datetime.now(timezone.utc)
    # The day-log's clock, so a journal entry and that moment's turns fall on
    # the same day whatever zone is configured.
    now_local = now_utc.astimezone(day_tz(root))

    # Journal uses cascading time structure; other branches use flat
    if branch == "journal" or branch.startswith("journal/"):
        entry_path = _journal_path(root, branch, now_local)
    else:
        entry_path = _flat_path(root, branch, now_local)

    # Folded to one line: a newline in either could forge frontmatter keys.
    heading = one_line(title or "") or f"Entry {now_local.strftime('%Y-%m-%d %H:%M')}"
    source = one_line(source_hint or "")

    # Build the entry block
    entry_lines = [
        "---",
        f"date: {now_local.strftime('%Y-%m-%d')}",
        f"time: {now_utc.strftime('%H:%M:%S')} UTC",
        f"branch: {branch}",
    ]
    if source:
        entry_lines.append(f"source: {source}")
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

    # Keep the index.md listings of the parent directories current.
    _update_index_pages(entry_path, root)

    return entry_path


def _append_entry(path: Path, entry_block: str) -> None:
    """Append an entry to a daily file, under the file's edit lock.

    Every open session's MCP server can write the same day's file. Unlocked,
    a writer that created the file could write over an entry another had
    just appended, and Windows appends aren't atomic between processes.
    """
    from smriti.core.files import edit_lock

    data = entry_block.encode("utf-8")
    with edit_lock(path), path.open("ab") as f:
        if f.tell():
            data = b"\n---\n\n" + data
        f.write(data)


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
    except Exception as exc:  # noqa: BLE001 — never blocks the write; the nightly catches up
        log.warning("Reindex after write failed: %s", exc)


def _update_index_pages(path: Path, root: Path) -> None:
    """Regenerate the parent index.md files; a failure never blocks the write."""
    try:
        from smriti.store.index_pages import update_index_pages

        updated = update_index_pages(path, root)
        if updated:
            log.info("Updated %d index pages", len(updated))
    except Exception as exc:  # noqa: BLE001 — an index-page failure never blocks the write
        log.warning("Index page update failed: %s", exc)

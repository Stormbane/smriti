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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from smriti.core.frontmatter import one_line

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Written:
    """Where an entry went, and how far it is searchable."""

    path: Path
    indexed: bool = False  # its chunks are in the index (at least for keyword search)
    vectors_pending: str = ""  # why its vectors are still to come, or ""
    index_error: str = ""  # why it isn't indexed at all yet, or ""

    def describe(self) -> str:
        """The outcome in a line, for the writer's caller (``smriti_write``)."""
        if self.vectors_pending:
            return (f"saved; indexed for keyword search; vectors pending "
                    f"({self.vectors_pending})")
        if self.index_error:
            return f"saved; not indexed yet ({self.index_error}); the next index run catches up"
        return "saved and indexed" if self.indexed else "saved"


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
    every segment must be a plain name by the rule every other path argument
    follows (:func:`smriti.core.safe_names.safe_name`): no ``.`` or ``..``, no
    dot-prefixed segment such as ``.git`` or ``.smriti``, no drive colon,
    reserved character or device name. Absolute paths, and paths that resolve
    outside the tree through a junction or symlink (e.g. ``mirrors/``), are
    rejected too. The error is :class:`UnsafeName`, a ``ValueError``.
    """
    from smriti.core.safe_names import UnsafeName, safe_name

    raw = branch.replace("\\", "/").strip()
    norm = raw.rstrip("/")
    if not norm or raw.startswith("/"):
        raise UnsafeName(f"invalid branch {branch!r}: use a relative path inside the memory tree")
    parts: list[str] = []
    for part in norm.split("/"):
        try:
            parts.append(safe_name(part, "segment"))
        except UnsafeName as exc:
            raise UnsafeName(f"invalid branch {branch!r}: {exc}") from exc
    norm = "/".join(parts)
    base = root.resolve()
    if not (base / norm).resolve().is_relative_to(base):
        raise UnsafeName(f"invalid branch {branch!r}: resolves outside the memory tree")
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
    """Write a new markdown entry; returns its path (see :func:`write_entry_result`)."""
    return write_entry_result(content, branch=branch, title=title, source_hint=source_hint,
                              root=root, reindex=reindex).path


def write_entry_result(
    content: str,
    *,
    branch: str = "journal",
    title: str | None = None,
    source_hint: str | None = None,
    root: Path | None = None,
    reindex: bool = True,
) -> Written:
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

        Credential-shaped strings in ``content``, ``title`` and
        ``source_hint`` are masked before they reach the disk
        (:mod:`smriti.core.memwrite`).
    root:
        Tree root (defaults to ``tree_root()``).
    reindex:
        If True (default), run an incremental index after writing so the
        new entry is immediately searchable.

    Returns
    -------
    Written
        The absolute path of the written file, and how far it is indexed.
    """
    from smriti.core.tree import tree_root

    if root is None:
        root = tree_root()
    branch = _checked_branch(root, branch)

    # Use local time for file paths and dates (journal entries are human-facing).
    # Store UTC in frontmatter for precision.
    from smriti.core.dayzone import day_tz

    now_utc = datetime.now(timezone.utc)
    # The day-log's clock, so a journal entry and that moment's turns fall on
    # the same day whatever zone is configured.
    now_local = now_utc.astimezone(day_tz(root))

    # Journal uses cascading time structure; other branches use flat
    if branch == "journal" or branch.startswith("journal/"):
        entry_path = _journal_path(root, branch, now_local)
    else:
        entry_path = _flat_path(root, branch, now_local)

    # Strings shaped like credentials are masked before anything reaches the
    # disk, as capture masks them in each turn: the tree is committed and
    # pushed, and an agent writes down whatever it was shown, pasted keys too.
    # The whole entry is masked where it is written (smriti.core.memwrite);
    # the title and source are masked here as well, before they are folded,
    # since the source goes to the metrics log too.
    from smriti.core.memwrite import mask

    # Folded to one line: a newline in either could forge frontmatter keys.
    heading = (one_line(mask(title or ""))
               or f"Entry {now_local.strftime('%Y-%m-%d %H:%M')}")
    source = one_line(mask(source_hint or ""))

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
    from smriti.metrics import logger_for
    logger_for(root).log(
        "write_entry",
        path=str(entry_path.relative_to(root)),
        branch=branch,
        content_bytes=len(content),
        has_title=title is not None,
        source_hint=source,  # scrubbed: the metrics log persists too
    )

    written = Written(entry_path)
    if reindex:
        written = _reindex_one(entry_path, root)

    # Keep the index.md listings of the parent directories current.
    _update_index_pages(entry_path, root)

    return written


def _append_entry(path: Path, entry_block: str) -> None:
    """Append an entry to a daily file, masked, under the file's edit lock.

    Every open session's MCP server can write the same day's file. Unlocked,
    a writer that created the file could write over an entry another had
    just appended. Each entry after the first follows a rule; the append
    keeps the file's line ending (:func:`smriti.core.memwrite.append_memory`).
    """
    from smriti.core.memwrite import append_memory

    append_memory(path, entry_block, separator="\n---\n\n")


def _reindex_one(path: Path, root: Path) -> Written:
    """Index just the written file — the write path's fast lane.

    A full ``index_tree`` here meant every MCP server did a ~7000-file
    scan per write while contending for the one write lock (the
    "connection closed" era). ``index_file`` embeds only this entry's
    chunks in one short transaction. Without vectors it is still indexed
    for keyword search, its vectors pending; the nightly full pass remains
    the backstop for anything a failed index leaves behind.
    """
    try:
        from smriti.store.indexer import index_file_result

        result = index_file_result(path, root=root)
        log.info("Indexed written file %s (%d chunks)", path.name, result.chunks)
        return Written(path, indexed=True, vectors_pending=result.vectors_pending)
    except Exception as exc:  # noqa: BLE001 — never blocks the write; reported to its caller
        log.warning("Reindex after write failed: %s", exc)
        return Written(path, index_error=f"{type(exc).__name__}: {exc}")


def _update_index_pages(path: Path, root: Path) -> None:
    """Regenerate the parent index.md files; a failure never blocks the write."""
    try:
        from smriti.store.index_pages import update_index_pages

        updated = update_index_pages(path, root)
        if updated:
            log.info("Updated %d index pages", len(updated))
    except Exception as exc:  # noqa: BLE001 — an index-page failure never blocks the write
        log.warning("Index page update failed: %s", exc)

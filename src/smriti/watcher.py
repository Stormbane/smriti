"""FileWatcher integration — detect tree changes and queue work.

Wraps the vendored memsearch ``FileWatcher`` to watch the narada tree for
markdown file changes. On change:

1. Structural cascade (immediate, free) — regenerates parent ``index.md``
2. Queue work based on file type:
   - **Leaf files** (sources/, inbox/, events/, etc.) → queue ``ingest``
     (summarize + route + cascade)
   - **Non-leaf files** (concepts, projects, goals, etc.) → queue ``route``
     (link discovery only, no summarization)

Queue dedup on ``(type, path)`` prevents redundant work from rapid edits.
All queued work is processed by ``smriti sleep`` or ``smriti daemon``.

Usage::

    smriti watch          # standalone watcher (foreground)
    smriti daemon start   # watcher + queue processor in one process
"""

from __future__ import annotations

import logging
from pathlib import Path

from smriti._vendored.memsearch.watcher import FileWatcher
from smriti.core.tree import tree_root
from smriti.store.cascade import structural_cascade
from smriti.store.queue import QueueTask, enqueue
from smriti.store.router import is_leaf_path
from smriti.store.wake_summary import is_identity_file

log = logging.getLogger(__name__)


def _on_change(event_type: str, file_path: Path) -> None:
    """Handle a file change event."""
    root = tree_root()

    # Skip smriti's own system directory (prevents infinite loops)
    if ".smriti" in file_path.parts:
        return

    # Skip index.md — written by structural cascade, watching it would loop
    if file_path.name == "index.md":
        return

    # Only process markdown files
    if file_path.suffix != ".md":
        return

    try:
        rel = str(file_path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return

    log.info("File %s: %s", event_type, rel)

    if event_type in ("created", "modified"):
        # Structural cascade: always, immediate, no LLM
        updated = structural_cascade(file_path, root)
        if updated:
            log.info("Structural cascade updated %d index files", len(updated))

        # Queue wake-context rebuild if an identity file changed
        if is_identity_file(rel):
            enqueue(QueueTask(type="wake_summary", path=rel, priority=3), root=root)
            log.info("Queued wake_context rebuild (identity file changed: %s)", rel)

        # Queue async work based on file type
        if is_leaf_path(rel):
            enqueue(QueueTask(type="ingest", path=rel), root=root)
            log.info("Queued ingest for leaf: %s", rel)
        else:
            enqueue(QueueTask(type="route", path=rel), root=root)
            log.info("Queued route for: %s", rel)

        from smriti.metrics import get_logger
        get_logger().log(
            "watcher_event",
            event_type=event_type,
            path=rel,
            leaf=is_leaf_path(rel),
            indexes_updated=len(updated),
        )

    elif event_type == "deleted":
        structural_cascade(file_path, root)
        log.info("Structural cascade after deletion of %s", file_path.name)

        from smriti.metrics import get_logger
        get_logger().log("watcher_event", event_type=event_type, path=rel)


def start(root: Path | None = None) -> FileWatcher:
    """Start watching the narada tree for changes.

    Returns the FileWatcher instance (call ``.stop()`` to stop).
    """
    if root is None:
        root = tree_root()

    watcher = FileWatcher(
        paths=[str(root)],
        callback=_on_change,
        debounce_ms=2000,
    )
    watcher.start()
    log.info("Watching %s for changes", root)
    return watcher

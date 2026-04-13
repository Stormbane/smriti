"""FileWatcher integration — detect tree changes and queue cascade tasks.

Wraps the vendored memsearch ``FileWatcher`` to watch the narada tree for
markdown file changes. On change: run structural cascade (auto) and queue
cognitive cascade tasks (deferred).

Usage::

    smriti watch          # foreground, watches ~/.narada/ for changes
    watcher.start()       # programmatic
"""

from __future__ import annotations

import logging
from pathlib import Path

from smriti._vendored.memsearch.watcher import FileWatcher
from smriti.core.tree import tree_root
from smriti.store.cascade import queue_cognitive_cascade, structural_cascade

log = logging.getLogger(__name__)


def _on_change(event_type: str, file_path: Path) -> None:
    """Handle a file change event."""
    root = tree_root()

    # Skip .smriti system directory
    if ".smriti" in file_path.parts:
        return

    log.info("File %s: %s", event_type, file_path.relative_to(root))

    if event_type in ("created", "modified"):
        # Structural cascade: update parent index.md files
        updated = structural_cascade(file_path, root)
        if updated:
            log.info(
                "Structural cascade updated %d index files",
                len(updated),
            )

        # Queue cognitive cascade for upstream review
        all_changed = [file_path] + updated
        queued = queue_cognitive_cascade(all_changed, root)
        if queued:
            log.info("Queued %d cognitive cascade tasks", queued)

        from smriti.metrics import get_logger
        get_logger().log("watcher_event", event_type=event_type, path=str(file_path.relative_to(root)), cascades_triggered=len(updated), tasks_queued=queued)

    elif event_type == "deleted":
        # Just run structural cascade to update indexes
        structural_cascade(file_path, root)
        log.info("Structural cascade after deletion of %s", file_path.name)

        from smriti.metrics import get_logger
        get_logger().log("watcher_event", event_type=event_type, path=str(file_path.relative_to(root)), cascades_triggered=0, tasks_queued=0)


def start(root: Path | None = None) -> FileWatcher:
    """Start watching the narada tree for changes.

    Returns the FileWatcher instance (call ``.stop()`` to stop).
    """
    if root is None:
        root = tree_root()

    watcher = FileWatcher(
        paths=[str(root)],
        callback=_on_change,
        debounce_ms=2000,  # 2 second debounce — generous for batch writes
    )
    watcher.start()
    log.info("Watching %s for changes", root)
    return watcher

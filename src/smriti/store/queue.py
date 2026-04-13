"""Non-blocking task queue for smriti cascade and maintenance work.

Tasks are appended by writers, the file watcher, and the structural cascade.
They are processed by ``smriti process`` or during sleep cycles. The queue
file lives at ``~/.narada/.smriti/queue.json``.

Sleep pressure = ``pending_count()`` — the number of unprocessed tasks.
A high count signals that the system needs sleep (batch processing).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)


@dataclass
class QueueTask:
    """A single queued task."""

    type: str  # "reindex" | "structural_cascade" | "cognitive_cascade"
    path: str  # file that triggered this task
    parent: str | None = None  # parent MOC path (for cascade tasks)
    priority: int = 5  # 0 = low, 10 = urgent
    queued_at: str = ""
    status: str = "pending"  # pending | processing | done | failed
    error: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.queued_at:
            self.queued_at = datetime.now(timezone.utc).isoformat()
        if not self.id:
            # Simple ID: type + timestamp hash
            import hashlib

            raw = f"{self.type}:{self.path}:{self.queued_at}"
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:12]


def _queue_path(root: Path | None = None) -> Path:
    if root is None:
        root = tree_root()
    p = root / ".smriti" / "queue.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_queue(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")


def enqueue(task: QueueTask, *, root: Path | None = None) -> None:
    """Add a task to the queue."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)

    # Deduplicate: don't add if an identical pending task exists
    for t in tasks:
        if (
            t.get("type") == task.type
            and t.get("path") == task.path
            and t.get("parent") == task.parent
            and t.get("status") == "pending"
        ):
            log.debug("Duplicate task skipped: %s %s", task.type, task.path)
            return

    tasks.append(asdict(task))
    _save_queue(qpath, tasks)
    log.info("Enqueued: %s %s (priority=%d)", task.type, task.path, task.priority)
    from smriti.metrics import get_logger
    get_logger().log("queue_snapshot", **queue_summary(root=root))


def pending_count(*, root: Path | None = None) -> int:
    """Return the number of pending tasks. This is the sleep pressure signal."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)
    return sum(1 for t in tasks if t.get("status") == "pending")


def dequeue(n: int = 1, *, root: Path | None = None) -> list[QueueTask]:
    """Return up to *n* pending tasks, marking them as 'processing'."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)

    result = []
    for t in tasks:
        if t.get("status") == "pending" and len(result) < n:
            t["status"] = "processing"
            result.append(QueueTask(**{k: v for k, v in t.items() if k in QueueTask.__dataclass_fields__}))

    _save_queue(qpath, tasks)
    return result


def complete(task_id: str, *, error: str = "", root: Path | None = None) -> None:
    """Mark a task as done (or failed if error is set)."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)

    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = "failed" if error else "done"
            t["error"] = error
            break

    _save_queue(qpath, tasks)


def cleanup(*, root: Path | None = None) -> int:
    """Remove completed and failed tasks. Returns number removed."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)
    before = len(tasks)
    tasks = [t for t in tasks if t.get("status") == "pending" or t.get("status") == "processing"]
    _save_queue(qpath, tasks)
    return before - len(tasks)


def queue_summary(*, root: Path | None = None) -> dict[str, int]:
    """Return counts by status."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)
    counts: dict[str, int] = {}
    for t in tasks:
        s = t.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts

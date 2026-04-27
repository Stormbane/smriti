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
    """A single queued task.

    Known ``type`` values, in sleep-dispatch order:

        "summarize_pending"        -- file >=50KB needs .summary.md sidecar
        "ingest"                   -- leaf file needs consolidation into a concept
                                      page (drained as a batch via batch_consolidate).
                                      Historical alias: consolidate_pending.
        "synthesize_threads_pending" -- concepts changed since cutoff (path = ISO
                                      timestamp); Stage 3 produces threads doc(s)
        "extract_actionables_pending" -- threads doc needs Stage 4 actionables
                                      extraction (path = relpath to threads doc)
        "structural_cascade"       -- rare; normally runs synchronously on write
        "cognitive_cascade"        -- propagate a leaf/concept change upward
        "journal_rollup"           -- create week/month/year summary
        "wake_summary"             -- rebuild MEMORY.md briefing
        "route"                    -- non-leaf page, routing judge for cross-links
        "reindex"                  -- refresh FTS5/vector index

    Classification policy for incoming writes lives in
    ``smriti.store.watch_router.classify_write``. The sleep dispatcher in
    ``cli._cmd_sleep`` drains types in the order above.
    """

    type: str
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


def dequeue(
    n: int = 1,
    *,
    types: list[str] | None = None,
    root: Path | None = None,
) -> list[QueueTask]:
    """Return up to *n* pending tasks, marking them as 'processing'.

    Parameters
    ----------
    n:
        Maximum number of tasks to dequeue.
    types:
        Optional allow-list of QueueTask.type values. When provided, only
        tasks whose type is in this set are dequeued. Enables scoped drain
        (e.g. process only ``summarize_pending`` tasks this cycle).
    root:
        Tree root.
    """
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)

    allow = set(types) if types else None
    result = []
    for t in tasks:
        if t.get("status") != "pending":
            continue
        if allow is not None and t.get("type") not in allow:
            continue
        if len(result) >= n:
            break
        t["status"] = "processing"
        result.append(QueueTask(**{k: v for k, v in t.items() if k in QueueTask.__dataclass_fields__}))

    _save_queue(qpath, tasks)
    return result


def pending_by_type(*, root: Path | None = None) -> dict[str, int]:
    """Return {type: count} for pending tasks. Used for sleep-pressure telemetry."""
    qpath = _queue_path(root)
    tasks = _load_queue(qpath)
    counts: dict[str, int] = {}
    for t in tasks:
        if t.get("status") != "pending":
            continue
        tp = t.get("type", "unknown")
        counts[tp] = counts.get(tp, 0) + 1
    return counts


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


def scope(
    *,
    keep: str | None = None,
    drop: str | None = None,
    types: list[str] | None = None,
    dry_run: bool = False,
    root: Path | None = None,
) -> dict[str, int]:
    """Scope the queue to a path-regex window for a given set of task types.

    Replaces the per-stage filter scripts under scripts/filter_queue_for_*.py.
    Loads the queue, drops pending tasks matching the criteria, writes back.

    Parameters
    ----------
    keep:
        Regex. Pending tasks whose ``path`` does NOT match are dropped. None = no keep filter.
    drop:
        Regex. Pending tasks whose ``path`` matches are dropped. None = no drop filter.
    types:
        Allow-list of task ``type`` values to apply the filter to. Tasks of
        types NOT in this list are kept untouched (this is the explicit
        non-regression behaviour the per-stage filter scripts had: they only
        ever pruned ``ingest`` tasks). None = apply to all types.
    dry_run:
        If True, return counts without modifying the queue file.
    root:
        Tree root.

    Returns
    -------
    dict with keys ``removed``, ``kept_in_scope``, ``kept_other``, ``total``.
    """
    import re as _re

    if keep is None and drop is None:
        raise ValueError("scope() requires at least one of keep= or drop=")

    keep_re = _re.compile(keep) if keep else None
    drop_re = _re.compile(drop) if drop else None
    type_set = set(types) if types else None

    qpath = _queue_path(root)
    tasks = _load_queue(qpath)
    new_tasks: list[dict[str, Any]] = []
    removed = 0
    kept_in_scope = 0
    kept_other = 0

    for t in tasks:
        if t.get("status") != "pending":
            new_tasks.append(t)
            kept_other += 1
            continue
        if type_set is not None and t.get("type") not in type_set:
            new_tasks.append(t)
            kept_other += 1
            continue
        path = t.get("path", "")
        in_scope = True
        if keep_re is not None and not keep_re.search(path):
            in_scope = False
        if drop_re is not None and drop_re.search(path):
            in_scope = False
        if in_scope:
            new_tasks.append(t)
            kept_in_scope += 1
        else:
            removed += 1

    if not dry_run:
        _save_queue(qpath, new_tasks)

    return {
        "removed": removed,
        "kept_in_scope": kept_in_scope,
        "kept_other": kept_other,
        "total": len(new_tasks),
    }

"""Pipeline introspection: what *should* be queued that isn't?

Each pipeline exposes a ``find_pending`` function that scans the
filesystem (and sometimes the index db) and returns the list of
``QueueTask`` objects that would be enqueued on a fresh system.

Two user-facing operations:

- ``audit(root)`` -- read-only; returns a dict of gap lists per pipeline.
  On a healthy system this is empty. Non-empty = a pipeline failed silently.
- ``rebuild(root)`` -- enqueue everything ``audit`` finds. Dedup via
  existing ``enqueue()`` logic. Use when recovering from queue loss
  or after a long absence.

State tracking: ``~/.narada/.smriti/pipeline_state.json`` holds per-pipeline
``last_run`` timestamps (ISO UTC). Handlers update their entry on completion.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from smriti.core.tree import smriti_db_path, tree_root
from smriti.store.cascade import PROTECTED_FILES
from smriti.store.queue import QueueTask
from smriti.store.router import LEAF_PREFIXES
from smriti.store.watch_router import (
    SUMMARIZE_THRESHOLD_BYTES,
    summary_sidecar_path,
)

log = logging.getLogger(__name__)


# ── Pipeline state ─────────────────────────────────────────────────


def _state_path(root: Path | None = None) -> Path:
    if root is None:
        root = tree_root()
    return root / ".smriti" / "pipeline_state.json"


def load_state(root: Path | None = None) -> dict:
    """Load pipeline_state.json. Returns {} if absent."""
    p = _state_path(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict, root: Path | None = None) -> None:
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def mark_run(pipeline: str, root: Path | None = None) -> None:
    """Record that `pipeline` completed now. Called by handlers on success."""
    state = load_state(root)
    state.setdefault("last_run", {})[pipeline] = (
        datetime.now(timezone.utc).isoformat()
    )
    save_state(state, root)


def last_run(pipeline: str, root: Path | None = None) -> datetime | None:
    """Return last successful run time for `pipeline`, or None."""
    state = load_state(root)
    ts = state.get("last_run", {}).get(pipeline)
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ── Filesystem helpers ─────────────────────────────────────────────


def _iter_markdown(root: Path):
    """Walk `root` yielding .md paths, skipping .smriti, .git, index.md, summary sidecars."""
    excluded_parts = {".smriti", ".git"}
    for p in root.rglob("*.md"):
        if any(part in excluded_parts for part in p.parts):
            continue
        if p.name == "index.md":
            continue
        if p.name.endswith(".summary.md"):
            continue
        yield p


def _rel(path: Path, root: Path) -> str | None:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return None


def _is_leaf(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in LEAF_PREFIXES)


# ── find_pending_summarize ─────────────────────────────────────────


def find_pending_summarize(root: Path | None = None) -> list[QueueTask]:
    """Return tasks for files >=SUMMARIZE_THRESHOLD_BYTES without a fresh sidecar.

    Mirrors/ excluded — junction-target pollution of real project dirs.
    """
    if root is None:
        root = tree_root()

    tasks: list[QueueTask] = []
    for path in _iter_markdown(root):
        if path.name in PROTECTED_FILES:
            continue
        rel = _rel(path, root)
        if rel is None or rel.startswith("mirrors/"):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < SUMMARIZE_THRESHOLD_BYTES:
            continue
        sidecar = summary_sidecar_path(path)
        if sidecar.exists():
            try:
                if sidecar.stat().st_mtime >= path.stat().st_mtime:
                    continue
            except OSError:
                pass
        tasks.append(
            QueueTask(type="summarize_pending", path=rel, priority=4)
        )
    return tasks


# ── find_pending_ingest (consolidate) ──────────────────────────────


def find_pending_ingest(root: Path | None = None) -> list[QueueTask]:
    """Leaf files not yet consolidated, or whose sha256 has changed since.

    Uses the source registry at ``.smriti/consolidated.json`` maintained by
    ``batch_consolidate``. A path is pending if:
      - not in the registry (never consolidated), OR
      - current sha256 differs from registry sha256 (edited since).

    First audit on a fresh system reveals the full backlog because the
    registry starts empty -- matches the design intent.
    """
    from smriti.store.source_registry import file_sha256, load_registry

    if root is None:
        root = tree_root()

    registry = load_registry(root)

    tasks: list[QueueTask] = []
    for path in _iter_markdown(root):
        rel = _rel(path, root)
        if rel is None or not _is_leaf(rel):
            continue
        if rel.startswith("mirrors/"):
            # Junctions into other projects' working trees; not smriti's content.
            continue
        if "/archive/" in rel or rel.endswith("/archive"):
            # Already-triaged content; preserved for audit trail, not for re-ingest.
            continue
        stored_sha = registry.get(rel)
        if stored_sha is not None:
            current_sha = file_sha256(path)
            if current_sha == stored_sha:
                continue  # already consolidated at this content version
        tasks.append(QueueTask(type="ingest", path=rel, priority=5))
    return tasks


# ── find_pending_journal_rollup ────────────────────────────────────


_JOURNAL_DAILY_RE = re.compile(r"^\d{2}-\d{2}\.md$")


def find_pending_journal_rollup(root: Path | None = None) -> list[QueueTask]:
    """Missing week/month/year summaries, or summary older than newest child."""
    if root is None:
        root = tree_root()

    tasks: list[QueueTask] = []
    journal_root = root / "journal"
    if not journal_root.exists():
        return tasks

    for year_dir in sorted(journal_root.iterdir()):
        if not year_dir.is_dir() or not re.match(r"^\d{4}$", year_dir.name):
            continue

        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not re.match(r"^\d{2}$", month_dir.name):
                continue

            # Week summaries
            for week_dir in sorted(month_dir.iterdir()):
                if not week_dir.is_dir() or not week_dir.name.startswith("week"):
                    continue
                week_summary = week_dir / f"{week_dir.name}.md"
                daily_files = [
                    f for f in week_dir.glob("*.md")
                    if _JOURNAL_DAILY_RE.match(f.name)
                ]
                if not daily_files:
                    continue
                if _summary_stale(week_summary, daily_files):
                    tasks.append(
                        QueueTask(
                            type="journal_rollup",
                            path=_rel(week_summary, root),
                            priority=3,
                        )
                    )

            # Month summary
            month_summary = month_dir / f"{month_dir.name}.md"
            week_summaries = [
                wd / f"{wd.name}.md"
                for wd in month_dir.iterdir()
                if wd.is_dir() and wd.name.startswith("week")
            ]
            week_summaries = [w for w in week_summaries if w.exists()]
            if week_summaries and _summary_stale(month_summary, week_summaries):
                tasks.append(
                    QueueTask(
                        type="journal_rollup",
                        path=_rel(month_summary, root),
                        priority=2,
                    )
                )

        # Year summary
        year_summary = year_dir / f"{year_dir.name}.md"
        month_summaries = [
            md / f"{md.name}.md"
            for md in year_dir.iterdir()
            if md.is_dir() and re.match(r"^\d{2}$", md.name)
        ]
        month_summaries = [m for m in month_summaries if m.exists()]
        if month_summaries and _summary_stale(year_summary, month_summaries):
            tasks.append(
                QueueTask(
                    type="journal_rollup",
                    path=_rel(year_summary, root),
                    priority=1,
                )
            )

    return tasks


def _summary_stale(summary: Path, children: list[Path]) -> bool:
    """True if summary is missing or older than any child."""
    if not summary.exists():
        return True
    try:
        summary_mtime = summary.stat().st_mtime
        return any(c.stat().st_mtime > summary_mtime for c in children)
    except OSError:
        return True


# ── find_pending_reindex ───────────────────────────────────────────


def find_pending_reindex(root: Path | None = None) -> list[QueueTask]:
    """Files on disk whose mtime > indexed_at in the chunks db, or missing entirely."""
    if root is None:
        root = tree_root()

    db_path = smriti_db_path()
    if not db_path.exists():
        # No index yet -- everything is pending reindex.
        return [
            QueueTask(type="reindex", path=_rel(p, root) or "", priority=1)
            for p in _iter_markdown(root)
        ]

    # Read indexed sources + max indexed_at per source
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT source, MAX(indexed_at) FROM chunks GROUP BY source"
        ).fetchall()
    finally:
        conn.close()

    indexed: dict[str, float] = {}
    for source, indexed_at in rows:
        if not indexed_at:
            continue
        try:
            indexed[source] = datetime.fromisoformat(indexed_at).timestamp()
        except ValueError:
            continue

    tasks: list[QueueTask] = []
    for path in _iter_markdown(root):
        rel = _rel(path, root)
        if rel is None:
            continue
        try:
            fs_mtime = path.stat().st_mtime
        except OSError:
            continue
        db_ts = indexed.get(rel) or indexed.get(rel.replace("/", "\\"))
        if db_ts is None or fs_mtime > db_ts:
            tasks.append(QueueTask(type="reindex", path=rel, priority=1))
    return tasks


# ── find_pending_cognitive_cascade ─────────────────────────────────


def find_pending_cognitive_cascade(root: Path | None = None) -> list[QueueTask]:
    """Pages with newer children (via wikilinks) than themselves.

    Walks the wikilink graph inverse: for every file that contains
    [[X]], if X.mtime > file.mtime, file should be cascaded from X.
    Expensive on large trees -- intended as an audit, not a hot path.
    """
    if root is None:
        root = tree_root()

    wikilink_re = re.compile(r"\[\[([^\]]+)\]\]")
    # Build: for each file, what does it wikilink to?
    links: dict[Path, list[Path]] = {}
    for path in _iter_markdown(root):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        targets: list[Path] = []
        for m in wikilink_re.finditer(content):
            target_rel = m.group(1).strip().rstrip("/")
            # Try with and without .md
            for candidate in (
                root / f"{target_rel}.md",
                root / target_rel,
            ):
                if candidate.exists() and candidate.is_file():
                    targets.append(candidate)
                    break
        if targets:
            links[path] = targets

    tasks: list[QueueTask] = []
    seen: set[str] = set()
    for parent_path, children in links.items():
        if parent_path.name in PROTECTED_FILES:
            continue
        try:
            parent_mtime = parent_path.stat().st_mtime
        except OSError:
            continue
        for child in children:
            try:
                child_mtime = child.stat().st_mtime
            except OSError:
                continue
            if child_mtime <= parent_mtime:
                continue
            rel = _rel(child, root)
            if rel is None or rel in seen:
                continue
            seen.add(rel)
            tasks.append(
                QueueTask(type="cognitive_cascade", path=rel, priority=5)
            )
            break  # one cascade per child triggers the walk
    return tasks


# ── find_pending_structural_cascade ────────────────────────────────


def find_pending_structural_cascade(root: Path | None = None) -> list[QueueTask]:
    """index.md files older than their newest non-index sibling."""
    if root is None:
        root = tree_root()

    tasks: list[QueueTask] = []
    for index_file in root.rglob("index.md"):
        if ".smriti" in index_file.parts or ".git" in index_file.parts:
            continue
        try:
            idx_mtime = index_file.stat().st_mtime
        except OSError:
            continue
        siblings = [
            s for s in index_file.parent.iterdir()
            if s.is_file() and s.suffix == ".md" and s.name != "index.md"
            and not s.name.endswith(".summary.md")
        ]
        if not siblings:
            continue
        newest = max(s.stat().st_mtime for s in siblings)
        if newest > idx_mtime:
            rel = _rel(index_file, root)
            if rel:
                # structural_cascade tasks key off the triggering child, not
                # the index itself; but for audit purposes pointing at the
                # index is what an operator wants to see.
                tasks.append(
                    QueueTask(type="structural_cascade", path=rel, priority=2)
                )
    return tasks


# ── audit + rebuild ────────────────────────────────────────────────


def find_pending_synthesize_threads(root: Path | None = None) -> list[QueueTask]:
    """Concepts modified since the most recent threads doc -> queue Stage 3.

    Heuristic: if there are concepts with mtime newer than the most recent
    file in semantic/threads/, those concepts are unsynthesized. Enqueue
    one synthesize_threads_pending task with cutoff = newest threads doc
    mtime (or epoch if no threads exist).
    """
    if root is None:
        root = tree_root()

    concept_dir = root / "semantic" / "concepts"
    threads_dir = root / "semantic" / "threads"
    if not concept_dir.exists():
        return []

    concepts = [
        p for p in concept_dir.glob("*.md")
        if p.name != "index.md" and not p.name.endswith(".summary.md")
    ]
    if not concepts:
        return []

    newest_threads_mtime = 0.0
    if threads_dir.exists():
        threads_files = list(threads_dir.glob("*-threads.md"))
        if threads_files:
            newest_threads_mtime = max(p.stat().st_mtime for p in threads_files)

    new_concepts = [c for c in concepts if c.stat().st_mtime > newest_threads_mtime]
    if len(new_concepts) < 2:
        return []  # need at least 2 for synthesis

    cutoff_iso = datetime.fromtimestamp(newest_threads_mtime, tz=timezone.utc).isoformat()
    return [
        QueueTask(
            type="synthesize_threads_pending",
            path=cutoff_iso,
            priority=4,
        )
    ]


def find_pending_extract_actionables(root: Path | None = None) -> list[QueueTask]:
    """Threads docs that don't have actionables extracted yet.

    Heuristic: a threads doc is "actioned" if it has been used as a source
    in any tasks.md / project todo (substring match on threads filename).
    Threads docs older than 24h with no such reference are likely missed.
    """
    if root is None:
        root = tree_root()

    threads_dir = root / "semantic" / "threads"
    if not threads_dir.exists():
        return []

    # Collect all task lines that source a threads doc, by reading
    # global tasks.md + project todos.
    sourced_in: set[str] = set()
    candidates: list[Path] = [root / "open-threads" / "tasks" / "tasks.md"]
    mirrors = root / "mirrors"
    if mirrors.exists():
        for sub in mirrors.iterdir():
            todo = sub / "ai" / "todo.md"
            if todo.exists():
                candidates.append(todo)

    for todo in candidates:
        try:
            text = todo.read_text(encoding="utf-8")
        except OSError:
            continue
        for tf in threads_dir.glob("*-threads.md"):
            if tf.stem in text:
                sourced_in.add(tf.name)

    tasks: list[QueueTask] = []
    cutoff = datetime.now(timezone.utc).timestamp() - 24 * 3600
    for tf in threads_dir.glob("*-threads.md"):
        if tf.name in sourced_in:
            continue
        try:
            mtime = tf.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            continue  # too fresh; might still get inline Stage 4
        rel = str(tf.relative_to(root)).replace("\\", "/")
        tasks.append(QueueTask(
            type="extract_actionables_pending",
            path=rel,
            priority=4,
        ))
    return tasks


PIPELINE_FINDERS = {
    "summarize": find_pending_summarize,
    "ingest": find_pending_ingest,
    "synthesize_threads": find_pending_synthesize_threads,
    "extract_actionables": find_pending_extract_actionables,
    "journal_rollup": find_pending_journal_rollup,
    "reindex": find_pending_reindex,
    "cognitive_cascade": find_pending_cognitive_cascade,
    "structural_cascade": find_pending_structural_cascade,
}


def audit(root: Path | None = None) -> dict[str, list[QueueTask]]:
    """Return per-pipeline pending task lists. Read-only."""
    if root is None:
        root = tree_root()
    result: dict[str, list[QueueTask]] = {}
    for pipeline, finder in PIPELINE_FINDERS.items():
        try:
            result[pipeline] = finder(root)
        except Exception as exc:
            log.warning("find_pending_%s failed: %s", pipeline, exc)
            result[pipeline] = []
    return result


def rebuild(root: Path | None = None) -> tuple[int, dict[str, int]]:
    """Enqueue everything audit finds. Returns (total_enqueued, per_pipeline)."""
    from smriti.store.queue import enqueue

    if root is None:
        root = tree_root()
    per_pipeline: dict[str, int] = {}
    total = 0
    for pipeline, tasks in audit(root).items():
        before = total
        for t in tasks:
            enqueue(t, root=root)
            total += 1
        per_pipeline[pipeline] = total - before
    return total, per_pipeline

"""smriti CLI — index and search the narada memory tree.

Usage::

    smriti index              # incremental index
    smriti index --full       # full re-index
    smriti read "query"       # search the tree
    smriti read "query" -n 10 # return 10 results
    smriti status             # show index stats
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def _cmd_index(args: argparse.Namespace) -> int:
    from smriti.store.indexer import index_tree

    stats = index_tree(
        full=args.full,
        root=Path(args.root) if args.root else None,
        verbose=args.verbose,
    )
    print(
        f"Scanned {stats['scanned']} files, "
        f"indexed {stats['indexed']}, "
        f"skipped {stats['skipped']}, "
        f"{stats['chunks']} chunks, "
        f"{stats['errors']} errors"
    )
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    import sqlite3

    from smriti.core.tree import smriti_db_path
    from smriti.store.search import search

    db_path = smriti_db_path()
    if not db_path.exists():
        print("No index found. Run 'smriti index' first.", file=sys.stderr)
        return 1

    # Re-open with extensions
    from smriti.store.schema import ensure_schema

    # Read dimension from existing db
    tmp = sqlite3.connect(str(db_path))
    row = tmp.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    tmp.close()
    if not row:
        print("Index is corrupted (no dimension). Run 'smriti index --full'.", file=sys.stderr)
        return 1

    dim = int(row[0])
    conn = ensure_schema(db_path, dim)

    query = " ".join(args.query)
    results = search(conn, query, top_k=args.n, use_reranker=not args.no_rerank)
    conn.close()

    if not results:
        print("No results found.")
        return 0

    for i, r in enumerate(results, 1):
        heading_part = f" :: {r.heading}" if r.heading else ""
        print(f"[{i}] {r.source}{heading_part} (score: {r.score:.2f}, depth: {r.trunk_distance})")
        # Show first 200 chars of content as preview
        preview = r.content[:200].replace("\n", " ").strip()
        if len(r.content) > 200:
            preview += "..."
        print(f"    {preview}")
        print()

    return 0


def _cmd_write(args: argparse.Namespace) -> int:
    import sys

    from smriti.store.writer import write_entry

    # Content from argument or stdin
    if args.content:
        content = " ".join(args.content)
    else:
        if sys.stdin.isatty():
            print("Reading from stdin (Ctrl+D to finish):", file=sys.stderr)
        content = sys.stdin.read()

    if not content.strip():
        print("Error: no content provided.", file=sys.stderr)
        return 1

    path = write_entry(
        content,
        branch=args.branch,
        title=args.title or None,
        source_hint=args.source or None,
        reindex=not args.no_index,
    )
    print(f"Written: {path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    import sqlite3

    from smriti.core.tree import smriti_db_path, tree_root

    db_path = smriti_db_path()
    root = tree_root()

    print(f"Tree root:  {root}")
    print(f"Database:   {db_path}")

    if not db_path.exists():
        print("Status:     Not indexed (run 'smriti index')")
        return 0

    conn = sqlite3.connect(str(db_path))
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    source_count = conn.execute("SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0]

    model_row = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
    model = model_row[0] if model_row else "unknown"

    dim_row = conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    dim = dim_row[0] if dim_row else "unknown"

    last_row = conn.execute("SELECT value FROM meta WHERE key = 'last_indexed'").fetchone()
    last = last_row[0] if last_row else "never"

    conn.close()

    print(f"Files:      {source_count}")
    print(f"Chunks:     {chunk_count}")
    print(f"Model:      {model} (dim={dim})")
    print(f"Indexed:    {last}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from smriti import watcher

    print("Watching ~/.narada/ for changes... (Ctrl+C to stop)")
    w = watcher.start()
    try:
        import time

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
        print("\nStopped.")
    return 0


def _cmd_sleep(args: argparse.Namespace) -> int:
    import time as _time

    from smriti.core.tree import tree_root
    from smriti.metrics import get_logger
    from smriti.store.cascade import cognitive_cascade
    from smriti.store.judge import executor_echo, judge_auto_keep
    from smriti.store.queue import complete, dequeue, pending_count

    root = tree_root()
    metrics = get_logger()
    count = pending_count()
    if count == 0:
        print("Queue empty — nothing to process. No sleep needed.")
        return 0

    n = count if args.all else min(args.n, count)
    print(f"Sleep cycle: processing {n} of {count} pending tasks...")

    t0 = _time.monotonic()
    metrics.log("sleep_started", pending_count=count, tasks_to_process=n)

    processed = 0
    failed = 0
    total_depth = 0
    tasks = dequeue(n)

    for task in tasks:
        print(f"  [{task.type}] {task.path}")
        try:
            if task.type == "cognitive_cascade":
                path = root / task.path
                if path.exists():
                    stats = cognitive_cascade(
                        path,
                        root,
                        judge_fn=judge_auto_keep,
                        executor_fn=executor_echo,
                    )
                    total_depth = max(total_depth, stats["max_depth"])
                    print(
                        f"    depth={stats['max_depth']}, "
                        f"verdicts={len(stats['verdicts'])}, "
                        f"changed={len(stats['files_changed'])}"
                    )
                else:
                    print("    skipped (file not found)")
            complete(task.id)
            processed += 1
        except Exception as exc:
            complete(task.id, error=str(exc))
            failed += 1
            print(f"    FAILED: {exc}")

    elapsed = int((_time.monotonic() - t0) * 1000)
    remaining = pending_count()

    metrics.log(
        "sleep_completed",
        tasks_processed=processed,
        tasks_failed=failed,
        elapsed_ms=elapsed,
        max_depth=total_depth,
    )

    print(f"\nSleep complete: {processed} processed, {failed} failed, {elapsed}ms.")
    print(f"{remaining} tasks remaining in queue.")
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    from smriti.store.queue import cleanup, pending_count, queue_summary

    if args.cleanup:
        removed = cleanup()
        print(f"Cleaned up {removed} completed/failed tasks.")
        return 0

    summary = queue_summary()
    total = sum(summary.values())
    pending = summary.get("pending", 0)

    print(f"Queue: {total} total, {pending} pending")
    for status, count in sorted(summary.items()):
        print(f"  {status}: {count}")

    if pending > 10:
        print(f"\nSleep pressure: HIGH ({pending} pending tasks)")
    elif pending > 0:
        print(f"\nSleep pressure: low ({pending} pending)")
    else:
        print("\nSleep pressure: none")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from smriti.eval.metrics import compute_metrics, enrich_from_metrics_log
    from smriti.eval.report import json_report, terminal_report
    from smriti.eval.runner import run_cascade_cases, run_judge_cases, run_search_cases

    judge_results = []
    search_results = []
    cascade_results = []

    if not args.search_only and not args.cascade_only:
        print("Running JUDGE cases...")
        judge_results = run_judge_cases()
    if not args.judge_only and not args.cascade_only:
        print("Running SEARCH cases...")
        try:
            search_results = run_search_cases()
        except RuntimeError as exc:
            print(f"  Skipped: {exc}")
    if not args.judge_only and not args.search_only:
        print("Running CASCADE cases...")
        cascade_results = run_cascade_cases()

    metrics = compute_metrics(judge_results, search_results, cascade_results)
    metrics = enrich_from_metrics_log(metrics)

    if args.json:
        print(json_report(metrics, judge_results, search_results, cascade_results))
    else:
        print(terminal_report(
            metrics, judge_results, search_results, cascade_results,
            verbose=args.verbose,
        ))

    if args.baseline:
        import json
        from smriti.core.tree import tree_root

        baseline_path = tree_root() / ".smriti" / "eval-baseline.json"
        baseline_path.write_text(
            json_report(metrics, judge_results, search_results, cascade_results),
            encoding="utf-8",
        )
        print(f"Baseline saved: {baseline_path}")

    return 0


def _cmd_metrics(args: argparse.Namespace) -> int:
    import json as _json

    from smriti.metrics import get_logger

    logger = get_logger()
    summary = logger.summary(since=args.since)

    if args.json:
        print(_json.dumps(summary, indent=2))
        return 0

    print(f"Metrics: {summary.get('period_start', '?')} → {summary.get('period_end', '?')}")
    print(f"  Events:      {summary.get('total_events', 0)}")
    for evt, count in sorted(summary.get("events_by_type", {}).items()):
        print(f"    {evt}: {count}")
    print(f"  Tokens in:   {summary.get('total_tokens_in', 0):,}")
    print(f"  Tokens out:  {summary.get('total_tokens_out', 0):,}")
    print(f"  Est. cost:   ${summary.get('total_cost_usd', 0):.4f}")
    print(f"  Searches:    {summary.get('search_count', 0)} (avg {summary.get('avg_search_ms', 0):.0f}ms)")
    print(f"  Index runs:  {summary.get('index_runs', 0)}")
    print(f"  Writes:      {summary.get('writes', 0)}")

    verdicts = summary.get("cascade_verdicts", {})
    if verdicts:
        print(f"  Verdicts:    {verdicts}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smriti",
        description="Index and search the narada memory tree.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    sub = parser.add_subparsers(dest="command")

    # ── index ────────────────────────────────────────────────────────
    p_index = sub.add_parser("index", help="Index the memory tree")
    p_index.add_argument("--full", action="store_true", help="Full re-index")
    p_index.add_argument("--root", type=str, default=None, help="Tree root path")

    # ── write ────────────────────────────────────────────────────────
    p_write = sub.add_parser("write", help="Write a new entry to the memory tree")
    p_write.add_argument("content", nargs="*", help="Entry text (omit to read from stdin)")
    p_write.add_argument("--branch", default="journal", help="Branch (default: journal)")
    p_write.add_argument("--title", type=str, default=None, help="Optional entry title")
    p_write.add_argument("--source", type=str, default=None, help="Provenance label")
    p_write.add_argument("--no-index", action="store_true", help="Skip re-indexing after write")

    # ── read ─────────────────────────────────────────────────────────
    p_read = sub.add_parser("read", help="Search the memory tree")
    p_read.add_argument("query", nargs="+", help="Search query")
    p_read.add_argument("-n", type=int, default=5, help="Number of results (default: 5)")
    p_read.add_argument("--no-rerank", action="store_true", help="Skip reranking")

    # ── status ───────────────────────────────────────────────────────
    sub.add_parser("status", help="Show index status")

    # ── watch ────────────────────────────────────────────────────────
    sub.add_parser("watch", help="Watch the tree for changes (foreground)")

    # ── sleep ────────────────────────────────────────────────────────
    p_sleep = sub.add_parser("sleep", help="Process queued cascade tasks (sleep cycle)")
    p_sleep.add_argument("--all", action="store_true", help="Process entire queue")
    p_sleep.add_argument("-n", type=int, default=1, help="Number of tasks to process")

    # ── queue ────────────────────────────────────────────────────────
    p_queue = sub.add_parser("queue", help="Show queue status")
    p_queue.add_argument("--cleanup", action="store_true", help="Remove completed tasks")

    # ── eval ─────────────────────────────────────────────────────────
    p_eval = sub.add_parser("eval", help="Run evaluation cases")
    p_eval.add_argument("--judge", dest="judge_only", action="store_true", help="JUDGE cases only")
    p_eval.add_argument("--search", dest="search_only", action="store_true", help="Search cases only")
    p_eval.add_argument("--cascade", dest="cascade_only", action="store_true", help="Cascade cases only")
    p_eval.add_argument("--json", action="store_true", help="JSON output")
    p_eval.add_argument("--baseline", action="store_true", help="Save results as baseline")

    # ── metrics ──────────────────────────────────────────────────────
    p_metrics = sub.add_parser("metrics", help="Show metrics summary")
    p_metrics.add_argument("--since", type=str, default=None, help="ISO timestamp filter")
    p_metrics.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    if args.command is None:
        parser.print_help()
        return 0

    handlers = {
        "index": _cmd_index,
        "write": _cmd_write,
        "read": _cmd_read,
        "status": _cmd_status,
        "watch": _cmd_watch,
        "sleep": _cmd_sleep,
        "queue": _cmd_queue,
        "eval": _cmd_eval,
        "metrics": _cmd_metrics,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

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
    from smriti.store.judge import (
        executor_echo,
        executor_via_claude,
        judge_auto_keep,
        judge_via_claude,
    )
    from smriti.store.queue import complete, dequeue, pending_count

    root = tree_root()
    metrics = get_logger()
    count = pending_count()
    if count == 0:
        print("Queue empty — nothing to process. No sleep needed.")
        return 0

    # Real judge/executor by default, stubs with --dry-run
    if args.dry_run:
        judge_fn = judge_auto_keep
        executor_fn = executor_echo
        mode = "dry-run (stubs)"
    else:
        judge_fn = judge_via_claude
        executor_fn = executor_via_claude
        mode = "claude -p"

    n = count if args.all else min(args.n, count)
    print(f"Sleep cycle: processing {n} of {count} pending tasks ({mode})...")

    t0 = _time.monotonic()
    metrics.log("sleep_started", pending_count=count, tasks_to_process=n, mode=mode)

    processed = 0
    failed = 0
    total_depth = 0
    total_verdicts = 0
    total_changed = 0
    tasks = dequeue(n)

    # Separate ingest tasks (batch) from others (per-task)
    ingest_tasks = [t for t in tasks if t.type == "ingest"]
    other_tasks = [t for t in tasks if t.type != "ingest"]

    # Batch consolidate ingest tasks
    if ingest_tasks:
        from smriti.store.consolidate import batch_consolidate

        ingest_paths = [root / t.path for t in ingest_tasks if (root / t.path).exists()]
        skipped_count = len(ingest_tasks) - len(ingest_paths)
        if skipped_count:
            print(f"  Skipped {skipped_count} ingest tasks (files not found)")

        if ingest_paths:
            print(f"  Batch consolidating {len(ingest_paths)} files...")
            results = batch_consolidate(ingest_paths, root, executor_fn=executor_fn)
            for r in results:
                page_rel = r.concept_page.relative_to(root) if r.concept_page else "(none)"
                print(f"    {r.action}: {page_rel} ({r.cluster_size} files)")
            total_changed += sum(1 for r in results if r.action in ("created", "revised"))

        for t in ingest_tasks:
            complete(t.id)
            processed += 1

    # Process other tasks individually
    for task in other_tasks:
        print(f"  [{task.type}] {task.path}")
        try:
            if task.type == "cognitive_cascade":
                path = root / task.path
                if path.exists():
                    stats = cognitive_cascade(
                        path,
                        root,
                        judge_fn=judge_fn,
                        executor_fn=executor_fn,
                    )
                    total_depth = max(total_depth, stats["max_depth"])
                    total_verdicts += len(stats["verdicts"])
                    total_changed += len(stats["files_changed"])
                    for v in stats["verdicts"]:
                        print(f"    {v['verdict']}: {v['parent']} — {v['reason']}")
                    if stats["promoted"]:
                        for p in stats["promoted"]:
                            print(f"    PROMOTE: {p} (needs human review)")
                    if stats["files_changed"]:
                        for f in stats["files_changed"]:
                            print(f"    REVISED: {f}")
                else:
                    print("    skipped (file not found)")
            elif task.type == "route":
                path = root / task.path
                if path.exists():
                    from smriti.store.router import route_file
                    result = route_file(path, root)
                    actions = result.get("actions_executed", [])
                    executed = sum(1 for a in actions if a.get("executed"))
                    print(f"    actions={len(actions)}, executed={executed}")
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
        total_verdicts=total_verdicts,
        total_changed=total_changed,
        mode=mode,
    )

    print(f"\nSleep complete: {processed} processed, {failed} failed, {elapsed}ms.")
    print(f"  Changed: {total_changed}, Max depth: {total_depth}")
    print(f"  {remaining} tasks remaining in queue.")
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


def _cmd_daemon(args: argparse.Namespace) -> int:
    """Unified daemon: file watcher + queue processor in one process."""
    import time as _time

    from smriti.core.tree import tree_root
    from smriti.metrics import get_logger
    from smriti.store.cascade import cognitive_cascade
    from smriti.store.judge import executor_via_claude, judge_via_claude
    from smriti.store.queue import complete, dequeue, pending_count
    from smriti.store.router import route_file

    if args.subcommand == "status":
        count = pending_count()
        print(f"Queue: {count} pending tasks")
        return 0

    # args.subcommand == "start"
    root = tree_root()
    metrics = get_logger()
    poll_interval = args.interval

    # Start file watcher unless --no-watch
    file_watcher = None
    if not args.no_watch:
        from smriti import watcher
        file_watcher = watcher.start(root)
        print(f"Watching {root} for changes")

    print(f"smriti daemon started (poll every {poll_interval}s, Ctrl+C to stop)")
    metrics.log("daemon_started", poll_interval=poll_interval, watch=not args.no_watch)

    processed_total = 0
    failed_total = 0

    try:
        while True:
            count = pending_count()
            if count == 0:
                _time.sleep(poll_interval)
                continue

            # Dequeue all available tasks
            tasks = dequeue(count)

            # Batch ingest tasks together
            ingest_tasks = [t for t in tasks if t.type == "ingest"]
            other_tasks = [t for t in tasks if t.type != "ingest"]

            if ingest_tasks:
                from smriti.store.consolidate import batch_consolidate

                ingest_paths = [root / t.path for t in ingest_tasks if (root / t.path).exists()]
                if ingest_paths:
                    print(f"  Batch consolidating {len(ingest_paths)} files...")
                    results = batch_consolidate(ingest_paths, root)
                    for r in results:
                        page_rel = r.concept_page.relative_to(root) if r.concept_page else "(none)"
                        print(f"    {r.action}: {page_rel} ({r.cluster_size} files)")
                for t in ingest_tasks:
                    complete(t.id)
                processed_total += len(ingest_tasks)

            for task in other_tasks:
                print(f"  [{task.type}] {task.path}")
                try:
                    if task.type == "cognitive_cascade":
                        path = root / task.path
                        if path.exists():
                            stats = cognitive_cascade(
                                path,
                                root,
                                judge_fn=judge_via_claude,
                                executor_fn=executor_via_claude,
                            )
                            print(
                                f"    depth={stats['max_depth']}, "
                                f"verdicts={len(stats['verdicts'])}, "
                                f"changed={len(stats['files_changed'])}"
                            )
                        else:
                            print("    skipped (file not found)")
                    elif task.type == "route":
                        path = root / task.path
                        if path.exists():
                            result = route_file(path, root)
                            actions = result.get("actions_executed", [])
                            executed = sum(1 for a in actions if a.get("executed"))
                            print(f"    actions={len(actions)}, executed={executed}")
                        else:
                            print("    skipped (file not found)")
                    complete(task.id)
                    processed_total += 1
                except Exception as exc:
                    complete(task.id, error=str(exc))
                    failed_total += 1
                    print(f"    FAILED: {exc}")

    except KeyboardInterrupt:
        if file_watcher:
            file_watcher.stop()
        print(f"\nDaemon stopped. Processed: {processed_total}, failed: {failed_total}")
        metrics.log("daemon_stopped", processed=processed_total, failed=failed_total)

    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from smriti.eval.metrics import compute_metrics, enrich_from_metrics_log
    from smriti.eval.report import json_report, terminal_report
    from smriti.eval.runner import run_cascade_cases, run_judge_cases, run_search_cases

    judge_fn = None
    if args.real:
        from smriti.store.judge import judge_via_claude
        judge_fn = judge_via_claude
        print("Using claude -p for JUDGE evaluation.")

    judge_results = []
    search_results = []
    cascade_results = []

    if not args.search_only and not args.cascade_only:
        print("Running JUDGE cases...")
        judge_results = run_judge_cases(judge_fn=judge_fn)
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


def _cmd_ingest(args: argparse.Namespace) -> int:
    from smriti.core.tree import tree_root
    from smriti.store.ingest import ingest
    from smriti.store.queue import QueueTask, enqueue

    sources = args.source if isinstance(args.source, list) else [args.source]
    root = tree_root()

    if args.queue:
        enqueued = 0
        for source in sources:
            src_path = Path(source).resolve()
            if not src_path.exists():
                print(f"skip (not found): {source}")
                continue
            try:
                rel = str(src_path.relative_to(root))
            except ValueError:
                # Source is outside the tree — store the absolute path instead
                rel = str(src_path)
            enqueue(QueueTask(type="ingest", path=rel), root=root)
            enqueued += 1
        print(f"Queued {enqueued} ingest task(s). Run 'smriti sleep' to process.")
        return 0

    # Direct ingest (original behavior)
    for source in sources:
        print(f"Ingesting: {source}")
        try:
            result = ingest(
                source,
                branch=args.branch,
                dry_run=args.dry_run,
                no_route=args.no_route,
                route_top_k=args.top_k,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}")
            return 1

        print(f"Source: {result.source} ({result.source_type})")
        if result.summary_path:
            print(f"Summary: {result.summary_path.relative_to(root)}")

        if result.routing.actions:
            print(f"Routing: {len(result.routing.actions)} actions")
            for action in result.routing.actions:
                prefix = "  DRY " if args.dry_run else "  "
                print(f"{prefix}{action.action:8s} {action.target} — {action.direction[:80]}")
        elif not args.no_route:
            print("Routing: no actions needed")

        if result.actions_executed:
            executed = sum(1 for a in result.actions_executed if a.get("executed"))
            promoted = sum(1 for a in result.actions_executed if a.get("action") == "PROMOTE")
            print(f"Executed: {executed} actions", end="")
            if promoted:
                print(f" ({promoted} promoted for human review)", end="")
            print()

        if result.cascade_queued:
            print(f"Cascade: queued {result.cascade_queued} tasks")

        print(f"Done ({result.elapsed_ms}ms)")

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
    p_sleep.add_argument("--dry-run", action="store_true", help="Use test stubs instead of claude -p")

    # ── queue ────────────────────────────────────────────────────────
    p_queue = sub.add_parser("queue", help="Show queue status")
    p_queue.add_argument("--cleanup", action="store_true", help="Remove completed tasks")

    # ── daemon ───────────────────────────────────────────────────────
    p_daemon = sub.add_parser("daemon", help="Watch for changes + process queue")
    p_daemon_sub = p_daemon.add_subparsers(dest="subcommand")
    p_daemon_start = p_daemon_sub.add_parser("start", help="Start watcher + queue processor")
    p_daemon_start.add_argument(
        "--interval", type=float, default=5.0,
        help="Queue poll interval in seconds (default: 5.0)"
    )
    p_daemon_start.add_argument(
        "--no-watch", action="store_true",
        help="Disable file watcher (queue processing only)"
    )
    p_daemon_sub.add_parser("status", help="Show queue status")
    p_daemon.set_defaults(subcommand="start", interval=5.0, no_watch=False)

    # ── eval ─────────────────────────────────────────────────────────
    p_eval = sub.add_parser("eval", help="Run evaluation cases")
    p_eval.add_argument("--judge", dest="judge_only", action="store_true", help="JUDGE cases only")
    p_eval.add_argument("--search", dest="search_only", action="store_true", help="Search cases only")
    p_eval.add_argument("--cascade", dest="cascade_only", action="store_true", help="Cascade cases only")
    p_eval.add_argument("--real", action="store_true", help="Use claude -p for JUDGE (default: stubs)")
    p_eval.add_argument("--json", action="store_true", help="JSON output")
    p_eval.add_argument("--baseline", action="store_true", help="Save results as baseline")

    # ── ingest ──────────────────────────────────────────────────────
    p_ingest = sub.add_parser("ingest", help="Ingest content into the memory tree")
    p_ingest.add_argument("source", nargs="+", help="File(s) or directory to ingest")
    p_ingest.add_argument("--branch", default="sources", help="Branch for summary (default: sources)")
    p_ingest.add_argument("--dry-run", action="store_true", help="Route but don't execute actions")
    p_ingest.add_argument("--no-route", action="store_true", help="Skip routing (summary only)")
    p_ingest.add_argument("-k", "--top-k", type=int, default=10, help="Routing candidates (default: 10)")
    p_ingest.add_argument("--queue", action="store_true",
                          help="Enqueue the source(s) for async ingest via 'smriti sleep' instead of running now")

    # ── metrics ──────────────────────────────────────────────────────
    p_metrics = sub.add_parser("metrics", help="Show metrics summary")
    p_metrics.add_argument("--since", type=str, default=None, help="ISO timestamp filter")
    p_metrics.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    # Wire private layer if vault already exists (idempotent)
    from smriti.core.tree import tree_root as _tree_root
    from smriti.private.store import PrivateStore as _PrivateStore
    _private_root = _tree_root() / "private"
    if _private_root.exists():
        _PrivateStore(_tree_root()).init()

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
        "daemon": _cmd_daemon,
        "eval": _cmd_eval,
        "metrics": _cmd_metrics,
        "ingest": _cmd_ingest,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

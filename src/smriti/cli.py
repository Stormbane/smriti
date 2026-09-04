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

    # Day-log capture health (per-source; scan recency defines liveness).
    try:
        from smriti.daylog.config import load_config as _daylog_config
        from smriti.daylog.state import DaylogState as _DaylogState

        _cfg = _daylog_config()
        if _cfg.state_path.exists():
            print("Day-log:")
            for name, h in sorted(_DaylogState.load(_cfg.state_path).health().items()):
                scan = h["scan_age_s"]
                scanning = "STALLED" if (scan == -1 or float(scan) > 600) else "scanning"
                print(
                    f"  {name}: {scanning} (last scan {scan}s ago, "
                    f"{h['files']} files, {h['parse_errors']} parse errors)"
                )
        else:
            print("Day-log:    not started (run 'smriti logd')")
    except Exception as exc:  # noqa: BLE001 — status must always print
        print(f"Day-log:    unavailable ({exc})")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from smriti.core.tree import tree_root
    from smriti.doctor import run_doctor

    memory_root = Path(args.memory_root).expanduser() if args.memory_root else tree_root()
    return run_doctor(
        harness=args.harness,
        memory_root=memory_root,
        project=Path(args.project).expanduser(),
    )


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


def _cmd_nightly(args: argparse.Namespace) -> int:
    from smriti.daylog.nightly import run_nightly

    status = run_nightly(rollup_cap=args.rollup_cap, reindex=not args.no_reindex)
    steps = status.get("steps", {})
    for name, step in steps.items():  # type: ignore[union-attr]
        if isinstance(step, dict):
            outcome = "ok" if step.get("ok", True) else f"FAILED: {step.get('error', '?')}"
            detail = {k: v for k, v in step.items() if k not in ("ok", "error")}
            print(f"  {name}: {outcome} {detail if detail else ''}")
    print(f"Nightly {'complete' if status.get('ok') else 'completed WITH FAILURES'} "
          f"for {status.get('target_day')}")
    return 0 if status.get("ok") else 1


def _cmd_morning(args: argparse.Namespace) -> int:
    from smriti.daylog.config import load_config
    from smriti.daylog.morning import run_morning

    cfg = load_config()
    if args.dry_run:
        from smriti.daylog.digest import compose
        from smriti.daylog.morning import _COMPOSE_ARGS, _SYSTEM  # noqa: PLC2701

        attempts: list[dict[str, str]] = []
        message = compose(_SYSTEM, "Yesterday's digest: (dry run — compose a sample)",
                          attempts, claude_cli_args=_COMPOSE_ARGS)
        print(message)
        return 0
    result = run_morning(cfg)
    if result.get("sent"):
        print(f"Morning message sent ({result.get('day')}).")
        return 0
    print(f"Morning message NOT sent: {result.get('reason', 'unknown')}")
    return 1


def _cmd_logd(args: argparse.Namespace) -> int:
    from smriti.daylog.logd import ensure_running, run_logd

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.ensure:
        ensure_running()
        return 0
    run_logd(interval_s=args.interval, max_passes=1 if args.once else None)
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    from smriti.daylog.schedule import install_tasks, remove_tasks, task_status

    if args.action == "install":
        return install_tasks()
    if args.action == "remove":
        return remove_tasks()
    return task_status()


def _cmd_sleep(args: argparse.Namespace) -> int:
    import time as _time

    if not getattr(args, "deep", False):
        print(
            "The deep pipeline (cascade / ingest / consolidate) is parked as future\n"
            "introspection work and no longer runs by default (spec: nightly-cycle).\n"
            "  - nightly maintenance:  smriti nightly\n"
            "  - run the deep pipeline anyway:  smriti sleep --deep [options]"
        )
        return 1

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

    from smriti.backup import trigger as _backup_trigger
    from smriti.store.queue import pending_by_type

    root = tree_root()
    _backup_trigger("sleep-start", push=True, root=root)
    metrics = get_logger()
    count = pending_count()
    if count == 0:
        print("Queue empty — nothing to process. No sleep needed.")
        return 0

    # Parse --types scope
    scoped_types: list[str] | None = None
    if getattr(args, "types", None):
        scoped_types = [t.strip() for t in args.types.split(",") if t.strip()]

    # Per-type queue snapshot (for logging and terminal visibility)
    by_type = pending_by_type(root=root)
    scoped_count = (
        sum(v for k, v in by_type.items() if k in scoped_types) if scoped_types else count
    )

    # Real judge/executor by default, stubs with --dry-run
    if args.dry_run:
        judge_fn = judge_auto_keep
        executor_fn = executor_echo
        mode = "dry-run (stubs)"
    else:
        judge_fn = judge_via_claude
        executor_fn = executor_via_claude
        mode = "claude -p"

    n = scoped_count if args.all else min(args.n, scoped_count)
    scope_label = f" [scope: {','.join(scoped_types)}]" if scoped_types else ""
    print(f"Sleep cycle: processing {n} of {scoped_count} pending tasks{scope_label} ({mode})")
    print(f"  Queue breakdown: {', '.join(f'{k}={v}' for k, v in sorted(by_type.items()))}")

    t0 = _time.monotonic()
    metrics.log(
        "sleep_started",
        pending_count=count,
        pending_by_type=by_type,
        tasks_to_process=n,
        scoped_types=scoped_types or [],
        mode=mode,
    )

    processed = 0
    failed = 0
    total_depth = 0
    total_verdicts = 0
    total_changed = 0
    tasks = dequeue(n, types=scoped_types)

    # Separate task types. Dispatch order below:
    #   summarize -> ingest -> cognitive_cascade -> journal_rollup
    #   -> wake_summary -> route -> reindex
    summarize_tasks = [t for t in tasks if t.type == "summarize_pending"]
    ingest_tasks = [t for t in tasks if t.type == "ingest"]
    journal_rollup_tasks = [t for t in tasks if t.type == "journal_rollup"]
    synth_threads_tasks = [t for t in tasks if t.type == "synthesize_threads_pending"]
    extract_actionables_tasks = [t for t in tasks if t.type == "extract_actionables_pending"]
    other_tasks = [
        t for t in tasks
        if t.type not in (
            "summarize_pending", "ingest", "journal_rollup",
            "synthesize_threads_pending", "extract_actionables_pending",
        )
    ]

    # Summarize first: produces .summary.md sidecars so downstream
    # pipelines (consolidate, cascade) can read the compact form.
    if summarize_tasks:
        from smriti.store.pipeline_audit import mark_run as _mark_run
        from smriti.store.summarize import batch_summarize

        summarize_paths = [
            root / t.path for t in summarize_tasks if (root / t.path).exists()
        ]
        skipped_count = len(summarize_tasks) - len(summarize_paths)
        if skipped_count:
            print(f"  Skipped {skipped_count} summarize tasks (files not found)")

        if summarize_paths:
            print(f"  Summarizing {len(summarize_paths)} large file(s)...")
            if args.dry_run:
                for p in summarize_paths:
                    print(f"    dry-run: would summarize {p.relative_to(root)}")
            else:
                s_results = batch_summarize(summarize_paths, root)
                for r in s_results:
                    rel = r.source.relative_to(root) if r.source.is_relative_to(root) else r.source
                    sidecar = r.sidecar.name if r.sidecar else "(none)"
                    print(f"    {r.action}: {rel} -> {sidecar}")
                total_changed += sum(1 for r in s_results if r.action in ("created", "refreshed"))

        for t in summarize_tasks:
            complete(t.id)
            processed += 1

        if not args.dry_run:
            try:
                _mark_run("summarize", root=root)
            except Exception as exc:
                logging.getLogger(__name__).warning("mark_run(summarize) failed: %s", exc)

    # Batch consolidate ingest tasks
    # Per-cluster commit: each cluster marks its source tasks complete
    # as soon as it finishes, so killing the process loses at most one
    # in-flight cluster instead of the whole drain.
    changed_concepts: list = []
    if ingest_tasks:
        from smriti.store.consolidate import batch_consolidate

        # Map source path -> task id so the callback can mark complete().
        task_id_by_path: dict[str, str] = {
            t.path.replace("\\", "/"): t.id for t in ingest_tasks
        }
        completed_ids: set[str] = set()

        ingest_paths = [root / t.path for t in ingest_tasks if (root / t.path).exists()]
        skipped_count = len(ingest_tasks) - len(ingest_paths)
        if skipped_count:
            print(f"  Skipped {skipped_count} ingest tasks (files not found)")

        def _on_cluster_done(r) -> None:
            """Per-cluster commit: mark each source file's queue task done
            (or failed if the cluster errored), track changed concept for
            downstream stages.

            Skipped clusters mark queue tasks as ``failed`` with the cluster
            error, NOT done. The source registry stays empty for those files,
            so the next ``queue rebuild`` re-enqueues them.
            """
            nonlocal processed
            cluster_failed = r.action not in ("created", "revised")
            err = (r.error or f"cluster {r.action}") if cluster_failed else ""
            for src in r.files:
                try:
                    rel = str(src.relative_to(root)).replace("\\", "/")
                except ValueError:
                    continue
                tid = task_id_by_path.get(rel)
                if tid and tid not in completed_ids:
                    complete(tid, error=err)
                    completed_ids.add(tid)
                    processed += 1
            if r.concept_page is not None and not cluster_failed:
                changed_concepts.append(r.concept_page)
            page_rel = r.concept_page.relative_to(root) if r.concept_page else "(none)"
            print(f"    {r.action}: {page_rel} ({r.cluster_size} files)", flush=True)

        if ingest_paths:
            print(f"  Batch consolidating {len(ingest_paths)} files...", flush=True)

            # Soft budget: if --budget-minutes set, exit cluster loop after
            # 70% of budget elapses so Stages 3/4 + audit can still run.
            budget_minutes = getattr(args, "budget_minutes", None)
            budget_deadline_s = (
                budget_minutes * 60 * 0.7 if budget_minutes else None
            )

            def _within_budget() -> bool:
                if budget_deadline_s is None:
                    return True
                elapsed = _time.monotonic() - t0
                return elapsed < budget_deadline_s

            try:
                results = batch_consolidate(
                    ingest_paths, root,
                    executor_fn=executor_fn,
                    on_cluster_done=_on_cluster_done,
                    should_continue=_within_budget,
                )
                total_changed += sum(1 for r in results if r.action in ("created", "revised"))
                if budget_deadline_s and not _within_budget():
                    elapsed_min = (_time.monotonic() - t0) / 60
                    print(
                        f"  Soft budget reached at {elapsed_min:.1f}min "
                        f"(threshold: {budget_minutes * 0.7:.1f}min). "
                        f"Wrap-up stages will still run.",
                        flush=True,
                    )
            except Exception as exc:
                logging.getLogger(__name__).warning("batch_consolidate crashed: %s", exc)
                print(f"  batch_consolidate aborted: {exc}", flush=True)

        # Sweep up any ingest tasks the callback didn't reach (files that
        # didn't exist on disk, embedding failures, clusters skipped for
        # capacity, or batch_consolidate aborting mid-flight). Mark these
        # as failed (NOT done) so the registry stays empty for them and
        # the next `queue rebuild` re-flags them. Marking done would hide
        # work that never actually happened.
        for t in ingest_tasks:
            if t.id not in completed_ids:
                complete(t.id, error="not processed by batch_consolidate")
                processed += 1

        if not args.dry_run:
            try:
                from smriti.store.pipeline_audit import mark_run as _mark_run
                _mark_run("ingest", root=root)
            except Exception as exc:
                logging.getLogger(__name__).warning("mark_run(ingest) failed: %s", exc)

    # Stage 3 + Stage 4 run AFTER ingest tasks are completed in the queue.
    # That way if these stages crash or get killed by an outer timeout,
    # the consolidation work is still durably committed and a re-run won't
    # re-do clusters that already succeeded.
    #
    # Stage 3 chunks above ~15 concepts per run because the synthesis prompt
    # collapses on bigger inputs (claude -p returns meta-comments instead of
    # synthesis text). Stage 4 runs per chunk.
    threads_results: list = []
    if changed_concepts and not args.dry_run:
        try:
            from smriti.store.threads import synthesize_threads_chunked

            print(
                f"  Synthesizing threads from {len(changed_concepts)} concept(s)...",
                flush=True,
            )
            threads_results = synthesize_threads_chunked(changed_concepts, root)
            for tr in threads_results:
                if tr.threads_path:
                    print(f"    wrote: {tr.threads_path.relative_to(root)}", flush=True)
                    total_changed += 1
                elif tr.skipped_reason:
                    print(f"    skipped: {tr.skipped_reason}", flush=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("Stage 3 (threads) failed: %s", exc)
            print(f"  Stage 3 failed: {exc}", flush=True)

    # Drain any standalone synthesize_threads_pending tasks (recovery from
    # prior sleep cycles where Stage 3 didn't run inline). path = ISO cutoff
    # timestamp; processes concepts modified since cutoff.
    if synth_threads_tasks and not args.dry_run:
        try:
            from datetime import datetime as _dt
            from smriti.store.threads import synthesize_threads_chunked

            for task in synth_threads_tasks:
                try:
                    cutoff = _dt.fromisoformat(task.path.replace("Z", "+00:00"))
                    cutoff_ts = cutoff.timestamp()
                except (ValueError, AttributeError):
                    cutoff_ts = 0.0
                concept_dir = root / "semantic" / "concepts"
                concepts = [
                    p for p in concept_dir.glob("*.md")
                    if p.name != "index.md"
                    and not p.name.endswith(".summary.md")
                    and p.stat().st_mtime >= cutoff_ts
                ]
                print(
                    f"  [synthesize_threads_pending] cutoff={task.path}, "
                    f"{len(concepts)} concepts qualify",
                    flush=True,
                )
                tr_results = synthesize_threads_chunked(concepts, root)
                for tr in tr_results:
                    if tr.threads_path:
                        # Enqueue Stage 4 for each produced threads doc.
                        from smriti.store.queue import enqueue, QueueTask
                        rel = str(tr.threads_path.relative_to(root)).replace("\\", "/")
                        enqueue(QueueTask(
                            type="extract_actionables_pending",
                            path=rel, priority=4,
                        ), root=root)
                        print(f"    wrote: {rel} -> queued actionables", flush=True)
                        threads_results.append(tr)
                complete(task.id)
                processed += 1
        except Exception as exc:
            logging.getLogger(__name__).warning("synthesize_threads_pending drain failed: %s", exc)
            print(f"  synth_threads drain failed: {exc}", flush=True)

    # Drain extract_actionables_pending tasks. Same path as inline Stage 4
    # but for threads docs that were never followed up.
    if extract_actionables_tasks and not args.dry_run:
        try:
            from smriti.store.actionables import extract_actionables_via_tools
            for task in extract_actionables_tasks:
                threads_path = root / task.path
                if not threads_path.exists():
                    print(f"  [extract_actionables_pending] {task.path} missing; skipping", flush=True)
                    complete(task.id)
                    processed += 1
                    continue
                print(f"  [extract_actionables_pending] {task.path}...", flush=True)
                a_result = extract_actionables_via_tools(threads_path, root)
                if a_result.error:
                    print(f"    error: {a_result.error}", flush=True)
                else:
                    by_proj = ", ".join(
                        f"{p}={n}" for p, n in sorted(a_result.todos_added_by_project.items())
                    ) or "none"
                    print(
                        f"    todos={a_result.total_todos} ({by_proj}), "
                        f"suti-comms={a_result.suti_comms_added}, "
                        f"goal-proposals={a_result.goal_proposals_added}",
                        flush=True,
                    )
                complete(task.id)
                processed += 1
        except Exception as exc:
            logging.getLogger(__name__).warning("extract_actionables_pending drain failed: %s", exc)
            print(f"  actionables drain failed: {exc}", flush=True)

    if threads_results and not args.dry_run:
        # Tool-based actionables: claude -p with smriti_* MCP tools allowed,
        # decides what to record. NARADA_ACTIONABLES_LEGACY=1 reverts to the
        # 3-stage prompt pipeline.
        use_legacy = os.environ.get("NARADA_ACTIONABLES_LEGACY", "").strip() == "1"
        try:
            if use_legacy:
                from smriti.store.actionables import extract_actionables

                for tr in threads_results:
                    if not tr.threads_path:
                        continue
                    print(f"  Extracting actionables (legacy) from {tr.threads_path.name}...", flush=True)
                    a_result = extract_actionables(tr.threads_path, root)
                    if a_result.error:
                        print(f"    error: {a_result.error}", flush=True)
                        continue
                    print(
                        f"    insights={len(a_result.insights)}, "
                        f"actionables={len(a_result.actionables)}, "
                        f"to-projects={a_result.tasks_routed}, "
                        f"to-global={a_result.global_tasks_routed}, "
                        f"goal-proposals={a_result.proposals_written}",
                        flush=True,
                    )
            else:
                from smriti.store.actionables import extract_actionables_via_tools

                for tr in threads_results:
                    if not tr.threads_path:
                        continue
                    print(f"  Extracting actionables (tools) from {tr.threads_path.name}...", flush=True)
                    a_result = extract_actionables_via_tools(tr.threads_path, root)
                    if a_result.error:
                        print(f"    error: {a_result.error}", flush=True)
                        continue
                    by_proj = ", ".join(
                        f"{p}={n}" for p, n in sorted(a_result.todos_added_by_project.items())
                    ) or "none"
                    print(
                        f"    todos={a_result.total_todos} ({by_proj}), "
                        f"suti-comms={a_result.suti_comms_added}, "
                        f"goal-proposals={a_result.goal_proposals_added}, "
                        f"{a_result.elapsed_ms}ms",
                        flush=True,
                    )
                    if a_result.summary_text:
                        # First line of claude's summary, for quick eyeball.
                        first = a_result.summary_text.splitlines()[0][:200]
                        print(f"    summary: {first}", flush=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("Stage 4 (actionables) failed: %s", exc)
            print(f"  Stage 4 failed: {exc}", flush=True)

    # Process journal rollup tasks -- create summary files that don't exist.
    # Journal rollup uses summarize_via_claude (single-prompt), not
    # executor_via_claude (parent/direction/child). Let rollup pick its own
    # default; pass None (or the dry-run stub) rather than the cascade
    # executor.
    if journal_rollup_tasks:
        from smriti.store.journal_rollup import rollup as journal_rollup_fn

        rollup_executor_fn = None  # use journal_rollup's default
        if args.dry_run:
            # In dry-run mode we never actually call the LLM — rollup
            # returns early on dry_run=True — so executor_fn is unused.
            pass

        # Sort by priority (week first, then month, then year) so children
        # exist before parents try to read them
        journal_rollup_tasks.sort(key=lambda t: -t.priority)
        for task in journal_rollup_tasks:
            print(f"  [journal_rollup] {task.path}")
            try:
                result_path = journal_rollup_fn(
                    task.path, root=root, executor_fn=rollup_executor_fn, dry_run=args.dry_run,
                )
                if result_path:
                    print(f"    created: {result_path.relative_to(root)}")
                    total_changed += 1
                elif args.dry_run:
                    print("    dry-run: would create")
                else:
                    print("    skipped (no children found)")
                complete(task.id)
                processed += 1
            except Exception as exc:
                complete(task.id, error=str(exc))
                failed += 1
                print(f"    FAILED: {exc}")

    # Collect wake_summary tasks — only need to run rebuild once
    wake_summary_tasks = [t for t in other_tasks if t.type == "wake_summary"]
    remaining_tasks = [t for t in other_tasks if t.type != "wake_summary"]

    if wake_summary_tasks:
        print(f"  [wake_summary] rebuilding from {len(wake_summary_tasks)} identity file change(s)...")
        try:
            from smriti.store.wake_summary import rebuild as rebuild_wake_context
            # Wake summary uses summarize_via_claude (single-prompt). Pass
            # None to let rebuild pick the correct default.
            result_path = rebuild_wake_context(root=root, executor_fn=None, dry_run=args.dry_run)
            if result_path:
                print(f"    rebuilt: {result_path.relative_to(root)}")
                total_changed += 1
            elif args.dry_run:
                print("    dry-run: would rebuild")
            else:
                print("    skipped (no trunk files or rebuild failed)")
        except Exception as exc:
            failed += len(wake_summary_tasks)
            print(f"    FAILED: {exc}")
        for t in wake_summary_tasks:
            complete(t.id)
            processed += 1

    # Shared visited-set across cognitive_cascade tasks so multiple leaves
    # cascading into the same parent only revise that parent once.
    _cascade_visited: set[Path] = set()

    # Process other tasks individually
    for task in remaining_tasks:
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
                        visited=_cascade_visited,
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
    remaining_by_type = pending_by_type(root=root)

    # Per-type delta: how many of each type did we drain this cycle?
    drained_by_type: dict[str, int] = {}
    for t in tasks:
        drained_by_type[t.type] = drained_by_type.get(t.type, 0) + 1

    metrics.log(
        "sleep_completed",
        tasks_processed=processed,
        tasks_failed=failed,
        elapsed_ms=elapsed,
        max_depth=total_depth,
        total_verdicts=total_verdicts,
        total_changed=total_changed,
        mode=mode,
        drained_by_type=drained_by_type,
        remaining_by_type=remaining_by_type,
        remaining_total=remaining,
    )

    print(f"\nSleep complete: {processed} processed, {failed} failed, {elapsed}ms.")
    print(f"  Changed: {total_changed}, Max depth: {total_depth}")
    if drained_by_type:
        print(f"  Drained by type: {', '.join(f'{k}={v}' for k, v in sorted(drained_by_type.items()))}")
    print(f"  {remaining} tasks remaining in queue.")
    if remaining_by_type:
        print(f"  Remaining by type: {', '.join(f'{k}={v}' for k, v in sorted(remaining_by_type.items()))}")

    # End-of-sleep audit: on a healthy system this finds zero gaps.
    # Gaps mean a pipeline failed silently. Log loudly; don't auto-backfill
    # (that would paper over real breakage).
    try:
        from smriti.store.pipeline_audit import audit as _audit
        gaps = _audit(root)
        gap_total = sum(len(v) for v in gaps.values())
        if gap_total == 0:
            print("  Audit: healthy (zero pipeline gaps)")
        else:
            gap_lines = [f"{k}={len(v)}" for k, v in gaps.items() if v]
            print(f"  Audit: {gap_total} gap(s): {', '.join(gap_lines)}")
            print("    Run 'smriti queue rebuild' to re-enqueue, or investigate upstream.")
        metrics.log("sleep_audit", gap_total=gap_total, gaps={k: len(v) for k, v in gaps.items()})
    except Exception as exc:
        logging.getLogger(__name__).warning("end-of-sleep audit failed: %s", exc)

    # Refresh the recall index so newly-written entries are searchable.
    # Best-effort: a qmd failure must never break sleep — the cascade is
    # the primary product of this cycle.
    if not args.dry_run and (total_changed > 0 or processed > 0):
        try:
            _refresh_recall_index(metrics)
        except Exception as exc:
            logging.getLogger(__name__).warning("recall index refresh failed: %s", exc)

    _backup_trigger("sleep-end", push=True, root=root)

    return 0


def _refresh_recall_index(metrics) -> None:
    """Run `qmd update` + `qmd embed` so new memory writes become recall-able.

    Skips silently if qmd isn't installed. Logs the outcome to metrics.
    Total runtime on a small delta is typically a few seconds.
    """
    import subprocess as _sp
    import time as _t

    from smriti.recall.backends.qmd import _resolve_qmd_cmd

    cmd = _resolve_qmd_cmd()
    if not cmd:
        metrics.log("recall_index_refresh", skipped="qmd_not_found")
        return

    t0 = _t.monotonic()
    print("  Refreshing recall index (qmd update + embed) ...")
    update = _sp.run([*cmd, "update"], capture_output=True, text=True, timeout=300)
    if update.returncode != 0:
        print(f"    qmd update failed (rc={update.returncode}): "
              f"{(update.stderr or update.stdout or '').strip()[-200:]}")
        metrics.log(
            "recall_index_refresh",
            stage="update",
            returncode=update.returncode,
            elapsed_ms=int((_t.monotonic() - t0) * 1000),
        )
        return

    embed = _sp.run([*cmd, "embed"], capture_output=True, text=True, timeout=600)
    elapsed_ms = int((_t.monotonic() - t0) * 1000)
    if embed.returncode != 0:
        print(f"    qmd embed failed (rc={embed.returncode}): "
              f"{(embed.stderr or embed.stdout or '').strip()[-200:]}")
    else:
        # Tail one line of qmd's own status output for visibility.
        tail = (embed.stdout or "").strip().splitlines()[-1:] or [""]
        print(f"    {tail[0][:160]}")
    metrics.log(
        "recall_index_refresh",
        stage="embed",
        returncode=embed.returncode,
        elapsed_ms=elapsed_ms,
    )


def _cmd_queue(args: argparse.Namespace) -> int:
    from smriti.store.queue import cleanup, pending_count, queue_summary

    action = args.action
    if action is None:
        action = "cleanup" if args.cleanup else "status"

    if action == "cleanup":
        removed = cleanup()
        print(f"Cleaned up {removed} completed/failed tasks.")
        return 0

    if action == "audit":
        return _cmd_queue_audit()

    if action == "rebuild":
        return _cmd_queue_rebuild()

    if action == "scope":
        return _cmd_queue_scope(args)

    # status (default)
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


def _cmd_queue_audit() -> int:
    """Read-only gap report per pipeline. Zero gaps = healthy."""
    from smriti.store.pipeline_audit import audit

    result = audit()
    total = sum(len(tasks) for tasks in result.values())

    print("Pipeline audit:")
    for pipeline, tasks in result.items():
        marker = "  " if not tasks else "! "
        print(f"{marker}{pipeline:22s} {len(tasks):5d} pending")
        # Show up to 3 examples per pipeline
        for t in tasks[:3]:
            print(f"      - {t.path}")
        if len(tasks) > 3:
            print(f"      ... +{len(tasks) - 3} more")

    print()
    if total == 0:
        print("Healthy: zero gaps. The queue is tracking reality.")
    else:
        print(f"Gaps found: {total} items across {sum(1 for v in result.values() if v)} pipelines.")
        print("Run 'smriti queue rebuild' to enqueue everything above.")
    return 0


def _cmd_queue_rebuild() -> int:
    """Enqueue everything audit finds. Dedup via existing enqueue logic."""
    from smriti.store.pipeline_audit import rebuild

    total, per_pipeline = rebuild()
    print(f"Queue rebuild: enqueued {total} task(s)")
    for pipeline, count in per_pipeline.items():
        if count:
            print(f"  {pipeline:22s} +{count}")
    if total == 0:
        print("  Nothing to enqueue. Queue already reflects filesystem state.")
    return 0


def _cmd_queue_scope(args: argparse.Namespace) -> int:
    """Drop pending tasks not matching --keep / matching --drop, scoped by --types.

    Replaces the per-stage filter scripts (scripts/filter_queue_for_*.py).
    Use case: after `smriti queue rebuild`, scope to a single stage's path
    pattern so `smriti sleep` only drains that window.

    Example::

        smriti queue rebuild
        smriti queue scope --keep '^heartbeat/artifacts/2026-04-1[0-7][-_]' --types ingest
        smriti sleep --all --types ingest --budget-minutes 30
    """
    from smriti.store.queue import scope

    if not args.keep and not args.drop:
        print("Error: queue scope requires at least one of --keep <regex> or --drop <regex>.")
        return 2

    types = None
    if args.types:
        types = [s.strip() for s in args.types.split(",") if s.strip()]

    try:
        result = scope(
            keep=args.keep,
            drop=args.drop,
            types=types,
            dry_run=args.dry_run,
        )
    except (ValueError, __import__("re").error) as exc:
        print(f"Error: {exc}")
        return 2

    prefix = "Would remove" if args.dry_run else "Removed"
    print(f"{prefix} {result['removed']} out-of-scope pending task(s).")
    print(f"Kept {result['kept_in_scope']} in-scope pending task(s).")
    print(f"Untouched (other status / other types): {result['kept_other']}.")
    print(f"Total queue size: {result['total']}.")
    if args.dry_run:
        print("(dry-run — queue not modified)")
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

    p_doctor = sub.add_parser("doctor", help="Verify a harness bridge without changing state")
    p_doctor.add_argument("--harness", required=True, choices=["codex", "claude-code"])
    p_doctor.add_argument("--project", required=True, help="Project root to inspect")
    p_doctor.add_argument("--memory-root", default=None, help="Memory root (default: ~/.narada)")

    # ── watch ────────────────────────────────────────────────────────
    sub.add_parser("watch", help="Watch the tree for changes (foreground)")

    # ── sleep ────────────────────────────────────────────────────────
    p_sleep = sub.add_parser("sleep", help="Process queued cascade tasks (sleep cycle)")
    p_sleep.add_argument("--all", action="store_true", help="Process entire queue")
    p_sleep.add_argument("-n", type=int, default=1, help="Number of tasks to process")
    p_sleep.add_argument(
        "--types",
        type=str,
        default=None,
        help="Comma-separated task types to scope to (e.g. 'summarize_pending,ingest'). "
             "Default: all types.",
    )
    p_sleep.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="Soft time budget in minutes. When 70%% of budget elapses, the "
             "ingest cluster loop exits cleanly and Stages 3/4 + queue completion "
             "+ audit still run. Without this, hard kills (e.g. external timeout) "
             "lose Stages 3/4 and corrupt queue state.",
    )
    p_sleep.add_argument("--dry-run", action="store_true", help="Use test stubs instead of claude -p")
    p_sleep.add_argument(
        "--deep",
        action="store_true",
        help="Actually run the parked deep pipeline (default: refuse and point at 'smriti nightly')",
    )

    # ── nightly / morning / logd (the narrow cycle) ──────────────────
    p_nightly = sub.add_parser(
        "nightly", help="Sleep task: reconcile day-log, render, digest, rollups, reindex"
    )
    p_nightly.add_argument("--rollup-cap", type=int, default=2,
                           help="Max rollups built per night (default 2)")
    p_nightly.add_argument("--no-reindex", action="store_true",
                           help="Skip the index + recall refresh step")
    p_morning = sub.add_parser(
        "morning", help="Wake task: compose + send the one good-morning message"
    )
    p_morning.add_argument("--dry-run", action="store_true",
                           help="Compose a sample message without ledger or send")
    p_logd = sub.add_parser("logd", help="Run the day-log watcher daemon (foreground)")
    p_logd.add_argument("--interval", type=float, default=5.0,
                        help="Seconds between collection passes (default 5)")
    p_logd.add_argument("--once", action="store_true", help="Run a single pass and exit")
    p_logd.add_argument("--ensure", action="store_true",
                        help="Keepalive: start a detached daemon if none is running")
    p_tasks = sub.add_parser(
        "tasks", help="Manage the nightly-cycle scheduled tasks (logd keepalive, nightly, morning)"
    )
    p_tasks.add_argument("action", choices=["install", "remove", "status"], nargs="?",
                         default="status")

    # ── queue ────────────────────────────────────────────────────────
    p_queue = sub.add_parser(
        "queue",
        help="Queue status, audit (gap report), rebuild (enqueue gaps), scope (filter), or cleanup",
    )
    p_queue.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["status", "audit", "rebuild", "scope", "cleanup"],
        help="status (default) | audit (read-only gaps) | rebuild (enqueue gaps) | scope (drop pending tasks not matching --keep / matching --drop) | cleanup",
    )
    p_queue.add_argument(
        "--cleanup",
        action="store_true",
        help="(alias for 'queue cleanup')",
    )
    p_queue.add_argument(
        "--keep",
        type=str,
        default=None,
        help="(scope) regex. Pending tasks whose path does NOT match are dropped.",
    )
    p_queue.add_argument(
        "--drop",
        type=str,
        default=None,
        help="(scope) regex. Pending tasks whose path matches are dropped.",
    )
    p_queue.add_argument(
        "--types",
        type=str,
        default=None,
        help="(scope) comma-separated task types to apply --keep/--drop to. Other types untouched. Default: all types.",
    )
    p_queue.add_argument(
        "--dry-run",
        action="store_true",
        help="(scope) print counts without modifying the queue.",
    )

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

    # ── merge-concepts ───────────────────────────────────────────────
    p_merge = sub.add_parser(
        "merge-concepts",
        help="Find and destructively merge near-duplicate concept pages",
    )
    p_merge.add_argument("--dry-run", action="store_true", help="Report pairs without merging")
    p_merge.add_argument("--limit", type=int, default=None, help="Cap number of merges per run")

    # ── recall ────────────────────────────────────────────────────────
    p_recall = sub.add_parser(
        "recall",
        help="Associative recall: ad-hoc query, status, stats, index",
    )
    p_recall_sub = p_recall.add_subparsers(dest="recall_cmd")
    p_recall_query = p_recall_sub.add_parser("query", help="Run a recall query")
    p_recall_query.add_argument("text", help="Query text")
    p_recall_query.add_argument("-n", "--top-k", type=int, default=4)
    p_recall_sub.add_parser("status", help="Show recall config + backend availability")
    p_recall_stats = p_recall_sub.add_parser("stats", help="Aggregate stats over recall.jsonl")
    p_recall_stats.add_argument("--hours", type=float, default=None)
    p_recall_stats.add_argument("--top", type=int, default=10)
    p_recall_index = p_recall_sub.add_parser(
        "index", help="(Re)build the recall index (qmd embed)",
    )
    p_recall_index.add_argument("--force", action="store_true", help="Pass -f to qmd embed")
    p_recall_daemon = p_recall_sub.add_parser(
        "daemon", help="Start/stop/status of qmd's HTTP daemon",
    )
    p_recall_daemon.add_argument("action", choices=["start", "stop", "status"])

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
        "doctor": _cmd_doctor,
        "watch": _cmd_watch,
        "sleep": _cmd_sleep,
        "queue": _cmd_queue,
        "daemon": _cmd_daemon,
        "eval": _cmd_eval,
        "metrics": _cmd_metrics,
        "ingest": _cmd_ingest,
        "merge-concepts": _cmd_merge_concepts,
        "recall": _cmd_recall,
        "nightly": _cmd_nightly,
        "morning": _cmd_morning,
        "logd": _cmd_logd,
        "tasks": _cmd_tasks,
    }
    return handlers[args.command](args)


def _cmd_merge_concepts(args: argparse.Namespace) -> int:
    """Find and destructively merge near-duplicate concept pages."""
    from smriti.store.consolidate_merge import find_merge_pairs, merge_all

    if args.dry_run:
        pairs = find_merge_pairs()
        if not pairs:
            print("No merge candidates (all concept pages below threshold).")
            return 0
        print(f"Merge candidates: {len(pairs)} pair(s) at threshold")
        for p in pairs:
            print(f"  {p.winner.name} <- {p.loser.name}  (sim={p.similarity:.2f}, "
                  f"winner_sources={p.winner_sources}, loser_sources={p.loser_sources})")
        if args.limit:
            print(f"\nWith --limit {args.limit}, first {min(args.limit, len(pairs))} would execute.")
        return 0

    result = merge_all(dry_run=False, limit=args.limit)
    print(f"merge-concepts: examined {result.pairs_examined} pair(s), "
          f"merged {result.pairs_merged}, {len(result.errors)} errors, "
          f"{result.elapsed_ms}ms")
    for winner, loser in result.merged:
        print(f"  MERGED {winner} <- {loser}")
    for err in result.errors:
        print(f"  ERROR {err}")
    return 0 if not result.errors else 1


def _cmd_recall(args: argparse.Namespace) -> int:
    """Recall subcommands: query, status, stats, index."""
    sub = getattr(args, "recall_cmd", None)
    if sub == "query":
        from smriti.recall import load_config, run_recall
        cfg = load_config()
        cfg = type(cfg)(**{**cfg.__dict__, "top_k": args.top_k, "max_inject": args.top_k})
        response = run_recall(args.text, cfg=cfg)
        print(f"backend={response.backend}  elapsed={response.elapsed_ms}ms  "
              f"matches={len(response.matches)}  error={response.error or '-'}")
        for m in response.matches:
            snippet = m.snippet[:160].replace("\n", " ").strip()
            print(f"  {m.score:.2f}  {m.source}\n      {snippet}")
        return 0
    if sub == "status":
        from smriti.recall import load_config
        from smriti.recall.backends import qmd as qmd_be
        from smriti.recall.backends import smriti_be as smriti_be
        cfg = load_config()
        daemon_up = qmd_be.daemon_health(timeout_s=0.5)
        print(f"backend (configured): {cfg.backend}")
        print(f"  qmd available:    {qmd_be.is_available()}")
        print(f"  qmd daemon up:    {daemon_up}  ({cfg.qmd_url})")
        print(f"  smriti available: {smriti_be.is_available()}")
        print(f"threshold:    {cfg.threshold}")
        print(f"top_k:        {cfg.top_k}")
        print(f"max_inject:   {cfg.max_inject}")
        print(f"timeout_s:    {cfg.timeout_s}")
        print(f"qmd rerank:   {cfg.rerank}  (qmd's LLM reranker; off by default per #519)")
        print(f"trunk_alpha:  {cfg.trunk_alpha}  (0 disables trunk-distance rerank)")
        print(f"collection:   {cfg.collection}")
        print(f"intent:       {cfg.intent or '(none)'}")
        print(f"no_http:      {cfg.no_http}")
        print(f"log_path:     {cfg.log_path}  (exists: {cfg.log_path.exists()})")
        return 0
    if sub == "stats":
        from smriti.recall.stats import main as stats_main
        argv = []
        if args.hours is not None:
            argv += ["--hours", str(args.hours)]
        if args.top:
            argv += ["--top", str(args.top)]
        return stats_main(argv)
    if sub == "index":
        import shutil
        import subprocess
        from smriti.recall.backends.qmd import _resolve_qmd_cmd
        cmd = _resolve_qmd_cmd()
        if not cmd:
            print("qmd not found. install via: npm install -g @tobilu/qmd", file=sys.stderr)
            return 1
        memory_root = Path.home() / ".narada"
        if not memory_root.exists():
            print(f"memory tree not found at {memory_root}", file=sys.stderr)
            return 1
        existing = subprocess.run(
            [*cmd, "collection", "list"], capture_output=True, text=True,
        )
        if "narada" not in (existing.stdout or ""):
            print(f"creating qmd collection 'narada' for {memory_root} ...")
            r = subprocess.run(
                [*cmd, "collection", "add", str(memory_root), "--name", "narada"],
                text=True,
            )
            if r.returncode != 0:
                return r.returncode
        print("running qmd embed ...")
        embed_args = [*cmd, "embed"]
        if args.force:
            embed_args.append("-f")
        r = subprocess.run(embed_args, text=True)
        return r.returncode
    if sub == "daemon":
        from smriti.recall.backends import qmd as qmd_be
        if args.action == "status":
            up = qmd_be.daemon_health(timeout_s=1.0)
            print(f"qmd daemon: {'up' if up else 'down'}")
            return 0 if up else 1
        if args.action == "start":
            ok, msg = qmd_be.daemon_start()
            print(f"qmd daemon start: {msg}")
            return 0 if ok else 1
        if args.action == "stop":
            ok, msg = qmd_be.daemon_stop()
            print(f"qmd daemon stop: {msg}")
            return 0 if ok else 1
    print("usage: smriti recall {query|status|stats|index|daemon}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

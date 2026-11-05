"""smriti CLI — index and search the memory tree.

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
import traceback
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
    from smriti.core.tree import smriti_db_path
    from smriti.store.schema import open_readonly
    from smriti.store.search import search

    db_path = smriti_db_path()
    if not db_path.exists():
        print("No index found. Run 'smriti index' first.", file=sys.stderr)
        return 1

    # Read-only, like the MCP server: a reader that opened the index for
    # writing (ensure_schema runs DDL and upserts meta) could block writers.
    conn = open_readonly(db_path)
    try:
        if not conn.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone():
            print("Index is corrupted (no dimension). Run 'smriti index --full'.",
                  file=sys.stderr)
            return 1
        query = " ".join(args.query)
        results = search(conn, query, top_k=args.n, use_reranker=not args.no_rerank)
    finally:
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


def _cmd_nightly(args: argparse.Namespace) -> int:
    from smriti.daylog.nightly import run_nightly

    status = run_nightly(rollup_cap=args.rollup_cap, digest_cap=args.digest_cap,
                         reindex=not args.no_reindex, witness=not args.no_witness)
    steps = status.get("steps", {})
    if not isinstance(steps, dict):
        steps = {}
    for name, step in steps.items():
        if isinstance(step, dict):
            outcome = "ok" if step.get("ok", True) else f"FAILED: {step.get('error', '?')}"
            detail = {k: v for k, v in step.items() if k not in ("ok", "error")}
            print(f"  {name}: {outcome} {detail if detail else ''}")
    print(f"Nightly {'complete' if status.get('ok') else 'completed WITH FAILURES'} "
          f"for {status.get('target_day')}")
    return 0 if status.get("ok") else 1


def _cmd_witness(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from smriti.core.tree import tree_root
    from smriti.daylog.model import day_key, day_tz
    from smriti.witness import run_witness, verify

    root = tree_root()
    if args.action == "stamp":
        result = run_witness(root, day_key(datetime.now(tz=day_tz(root)).date()))
        detail = {k: v for k, v in result.items() if k not in ("ok", "error")}
        print(f"witness: {'ok' if result.get('ok') else 'FAILED: ' + str(result.get('error'))}"
              f" {detail}")
        return 0 if result.get("ok") else 1

    checks = verify(root, offline=args.offline)
    if not checks:
        print("witness: no stamped nights yet")
        return 1
    for c in checks:
        if c.anchor_ok and c.block_time:
            mined = datetime.fromtimestamp(c.block_time, timezone.utc).date().isoformat()
            anchor = f"anchored in Bitcoin block {c.block_height} ({mined})"
        elif c.block_height:
            anchor = f"anchored in block {c.block_height} (not checked)"
        else:
            anchor = "pending Bitcoin confirmation"
        state = "ok" if c.ok else "PROBLEM: " + "; ".join(c.problems)
        print(f"  {c.night}  {c.head[:7]}  {anchor}  {state}")
    broken = [c for c in checks if not c.ok]
    if broken:
        print(f"Witness found problems from {broken[0].night}.")
        return 1
    print(f"History intact across {len(checks)} night(s), "
          f"{checks[0].night} to {checks[-1].night}.")
    return 0


def _cmd_morning(args: argparse.Namespace) -> int:
    from smriti.daylog.config import load_config
    from smriti.daylog.morning import run_morning

    cfg = load_config()
    if args.dry_run:
        from smriti.daylog.digest import compose
        from smriti.daylog.morning import _COMPOSE_ARGS, _system_prompt  # noqa: PLC2701

        attempts: list[dict[str, str]] = []
        message = compose(_system_prompt(cfg),
                          "Yesterday's digest: (dry run — compose a sample)",
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


def _cmd_backup(args: argparse.Namespace) -> int:
    from smriti.backup import trigger

    trigger(args.tag, push=args.push)
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    from smriti.daylog.schedule import install_tasks, remove_tasks, task_status

    if args.action == "install":
        return install_tasks()
    if args.action == "remove":
        return remove_tasks()
    return task_status()


def _cmd_eval(args: argparse.Namespace) -> int:
    from smriti.eval.metrics import compute_metrics, enrich_from_metrics_log
    from smriti.eval.report import json_report, terminal_report
    from smriti.eval.runner import eval_sandbox, run_search_cases

    with eval_sandbox() as sandbox:
        print("Running SEARCH cases...")
        search_results = run_search_cases()

        metrics = compute_metrics(search_results)
        metrics = enrich_from_metrics_log(metrics)

        if args.json:
            print(json_report(metrics, search_results))
        else:
            print(terminal_report(metrics, search_results, verbose=args.verbose))

        if args.baseline:
            baseline_path = (
                sandbox / ".smriti" / "eval-baseline.json"
                if args.baseline is True else Path(args.baseline).resolve()
            )
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(json_report(metrics, search_results), encoding="utf-8")
            print(f"Baseline saved: {baseline_path}")
            if args.baseline is True:
                print("Temporary baseline is removed on exit; use --baseline PATH to keep it.")

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
    print(f"  Searches:    {summary.get('search_count', 0)} "
          f"(avg {summary.get('avg_search_ms', 0):.0f}ms)")
    print(f"  Index runs:  {summary.get('index_runs', 0)}")
    print(f"  Writes:      {summary.get('writes', 0)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smriti",
        description="Index and search the memory tree.",
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

    # ── nightly / morning / logd (the narrow cycle) ──────────────────
    p_nightly = sub.add_parser(
        "nightly", help="Sleep task: reconcile day-log, render, digest, rollups, reindex"
    )
    p_nightly.add_argument("--rollup-cap", type=int, default=2,
                           help="Max rollups built per night (default 2)")
    p_nightly.add_argument("--digest-cap", type=int, default=10,
                           help="Max day digests per night, newest first (default 10)")
    p_nightly.add_argument("--no-reindex", action="store_true",
                           help="Skip the index + recall refresh step")
    p_nightly.add_argument("--no-witness", action="store_true",
                           help="Skip the commit + timestamp step")
    p_witness = sub.add_parser(
        "witness", help="Tamper-evidence: timestamp the memory tree's HEAD, or verify history"
    )
    p_witness.add_argument("action", choices=["stamp", "verify"])
    p_witness.add_argument("--offline", action="store_true",
                           help="verify: skip checking proofs against Bitcoin")
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
    p_backup = sub.add_parser(
        "backup", help="Commit the memory tree now if it changed (the hourly task runs this)"
    )
    p_backup.add_argument("--push", action="store_true",
                          help="Also push to origin, when there is anything new")
    p_backup.add_argument("--tag", default="manual",
                          help="Commit message tag; keep it generic (default: manual)")
    p_tasks = sub.add_parser(
        "tasks", help="Manage the scheduled tasks (logd keepalive, hourly backup, nightly, morning)"
    )
    p_tasks.add_argument("action", choices=["install", "remove", "status"], nargs="?",
                         default="status")

    # ── eval ─────────────────────────────────────────────────────────
    p_eval = sub.add_parser(
        "eval", help="Run the search evaluation cases against a synthetic fixture"
    )
    p_eval.add_argument("--json", action="store_true", help="JSON output")
    p_eval.add_argument(
        "--baseline", nargs="?", const=True, metavar="PATH",
        help="Save baseline to PATH (default: disposable sandbox)",
    )

    # ── metrics ──────────────────────────────────────────────────────
    p_metrics = sub.add_parser("metrics", help="Show metrics summary")
    p_metrics.add_argument("--since", type=str, default=None, help="ISO timestamp filter")
    p_metrics.add_argument("--json", action="store_true", help="JSON output")

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

    if args.command is None:
        parser.print_help()
        return 0
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("smriti: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — the top-level handler reports, exits 1
        # One clean line and a failing exit code; the traceback is for -v.
        if args.verbose:
            traceback.print_exc()
        print(f"smriti {args.command}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if not args.verbose:
            print("Run with -v for the traceback.", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    # Eval must enter its sandbox before any default-root access.
    if args.command != "eval":
        # Wire private layer if vault already exists (idempotent)
        from smriti.core.tree import tree_root as _tree_root
        from smriti.private.store import PrivateStore as _PrivateStore
        _private_root = _tree_root() / "private"
        if _private_root.exists():
            _PrivateStore(_tree_root()).init()

    handlers = {
        "index": _cmd_index,
        "write": _cmd_write,
        "read": _cmd_read,
        "status": _cmd_status,
        "doctor": _cmd_doctor,
        "eval": _cmd_eval,
        "metrics": _cmd_metrics,
        "recall": _cmd_recall,
        "nightly": _cmd_nightly,
        "witness": _cmd_witness,
        "morning": _cmd_morning,
        "logd": _cmd_logd,
        "backup": _cmd_backup,
        "tasks": _cmd_tasks,
    }
    return handlers[args.command](args)


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

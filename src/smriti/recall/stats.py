"""Aggregate stats over the recall log (``recall.jsonl``).

Reports how often the recall hook fires, where time is spent, how often
a match is injected, and the rough token cost.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from statistics import mean, median

from smriti.recall.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=None,
                        help="Only include entries from the last N hours")
    parser.add_argument("--top", type=int, default=10,
                        help="Top-N file paths by frequency")
    parser.add_argument("--log-path", type=Path, default=None,
                        help="Override the recall log path")
    args = parser.parse_args(argv)

    log_path = args.log_path or load_config().log_path
    if not log_path.exists():
        print(f"No recall log at {log_path}")
        return 0

    cutoff = time.time() - (args.hours * 3600) if args.hours else 0.0

    rows: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("ts", 0) < cutoff:
            continue
        rows.append(r)

    if not rows:
        print("No entries in window.")
        return 0

    fired = len(rows)
    injected = sum(1 for r in rows if r.get("injected", 0) > 0)
    skipped = sum(1 for r in rows if "skip" in r)
    errored = sum(1 for r in rows if "error" in r and "skip" not in r)

    elapsed = [r.get("elapsed_ms", 0) for r in rows if r.get("elapsed_ms")]
    inj_tokens = sum(r.get("injected_tokens_est", 0) for r in rows)
    scores = [r.get("top_score", 0.0) for r in rows if r.get("top_score")]

    by_backend = Counter(r.get("backend", "?") for r in rows)
    by_tool = Counter(r.get("tool", "?") for r in rows)
    by_file = Counter(r.get("file_path", "") for r in rows if r.get("file_path"))
    by_top_source = Counter(
        r.get("top_source", "") for r in rows
        if r.get("injected", 0) > 0 and r.get("top_source")
    )

    window = f"last {args.hours}h" if args.hours else "all-time"
    print(f"=== Smriti recall stats ({window}) ===")
    print(f"Fires:           {fired}")
    print(f"  injected:      {injected} ({injected*100//max(fired,1)}%)")
    print(f"  skipped:       {skipped}")
    print(f"  errored:       {errored}")
    if elapsed:
        print(f"Latency ms:      median {int(median(elapsed))}, "
              f"mean {int(mean(elapsed))}, max {max(elapsed)}")
    print(f"Injected tokens: ~{inj_tokens} (rough estimate)")
    if scores:
        print(f"Top score:       median {median(scores):.2f}, "
              f"mean {mean(scores):.2f}")
    print()
    print(f"By backend: {dict(by_backend)}")
    print(f"By tool:    {dict(by_tool)}")
    print()
    print(f"=== Top {args.top} files (by recall fires) ===")
    for path, n in by_file.most_common(args.top):
        short = path if len(path) <= 80 else "..." + path[-77:]
        print(f"  {n:4d}  {short}")
    if by_top_source:
        print()
        print(f"=== Top {args.top} matched sources (when injected) ===")
        for source, n in by_top_source.most_common(args.top):
            print(f"  {n:4d}  {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

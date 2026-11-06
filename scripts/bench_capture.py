"""Time capture: collection passes over synthetic transcripts, for one or more checkouts.

    python scripts/bench_capture.py A [B ...] [--lines N] [--rounds R]
                                    [--codex legacy|paginated] [--zone offset|system]
                                    [--work DIR] [--json FILE]

Each side is a checkout directory or a git ref of this repository (exported
with ``git archive`` into ``--work``). Every measurement runs in a fresh
process with ``PYTHONPATH=<side>/src``, ``-B`` (so no bytecode is written
into a checkout) and ``SMRITI_ROOT`` set to a new directory under
``--work``: no live tree, capture state or transcript is read or written.
Sides alternate (A, B, B, A, ...) so drift on the machine falls on all of
them.

A round writes ``--lines`` Claude Code transcript lines and as many Codex
rollout lines, spread over 20 sessions each and three days, with the share
of records capture skips that real transcripts have (tool calls and their
output, progress and token-count records, injected context), all of it
synthetic (the user Sam, the agent Ari, a made-up project; a fixed seed).
Then, in that process, it times one ``collect_once`` pass that finds
everything new (read, extract, dedup, append, save the state), an idle
pass that finds nothing new, and a pass after about 2% more lines were
appended to every file. Every file is opened once before a timed pass:
the first open of a new or changed file pays for the virus scanner (about
11 ms each on Windows), which is no part of capture. The table gives each
side's median over its rounds and the turns each pass appended, which
must agree between sides doing the same work.

``--codex legacy`` (the default) writes the ``event_msg`` user and agent
messages every version reads; ``paginated`` writes the ``item_completed``
records Codex CLI writes since 0.153, which only code with H12.17 reads
(other sides then capture no Codex turn, so theirs is not the same work).
``--zone offset`` (the default) pins the day boundary to UTC+10 with
``SMRITI_DAY_UTC_OFFSET``, as every version can; ``system`` leaves it to
the system's zone.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SESSIONS = 20
WORDS = ("lamp garden ledger river window index digest morning capture branch tree "
         "kettle orchard harbour lantern quiet signal weather market thread pocket "
         "compass pebble meadow ribbon cedar").split()
START = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


def _text(rng: random.Random, low: int, high: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(low, high)))


def _stamp(i: int, n: int) -> str:
    ts = START + timedelta(seconds=i * (3 * 86400 / max(n, 1)))  # three days
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def claude_lines(rng: random.Random, session: str, n: int, first: int, total: int) -> list[str]:
    """Claude Code records: about a third conversation, the rest tool traffic and progress."""
    out: list[str] = []
    for k in range(n):
        i = first + k
        base = {"timestamp": _stamp(i, total), "sessionId": session,
                "cwd": "/home/sam/projects/orchard", "uuid": f"u-{session}-{i}"}
        kind = i % 6
        if kind == 0:
            rec = {**base, "type": "user", "message": {"role": "user",
                                                       "content": _text(rng, 8, 60)}}
        elif kind == 1:
            rec = {**base, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": _text(rng, 20, 80)},
                {"type": "text", "text": _text(rng, 20, 150)}]}}
        elif kind == 2:
            rec = {**base, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{i}", "name": "Read",
                 "input": {"file_path": "/home/sam/projects/orchard/notes.md"}}]}}
        elif kind == 3:
            rec = {**base, "type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}",
                 "content": _text(rng, 150, 600)}]}}
        elif kind == 4:
            rec = {**base, "type": "progress", "data": {"message": _text(rng, 5, 20)}}
        else:
            rec = {**base, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": _text(rng, 10, 80)}]}}
        out.append(json.dumps(rec))
    return out


def codex_lines(rng: random.Random, fmt: str, n: int, first: int, total: int) -> list[str]:
    """Codex rollout records after session_meta: messages, tool calls, token counts."""
    out: list[str] = []
    for k in range(n):
        i = first + k
        ts = _stamp(i, total)
        kind = i % 6
        if kind == 0:
            text = _text(rng, 8, 60)
            if fmt == "legacy":
                payload: dict[str, Any] = {"type": "user_message", "message": text,
                                           "images": [], "local_images": []}
            else:
                payload = {"type": "item_completed", "thread_id": "t", "turn_id": f"turn{i}",
                           "item": {"type": "UserMessage", "id": f"m{i}", "content": [
                               {"type": "text", "text": text, "text_elements": []}]}}
            rec: dict[str, Any] = {"timestamp": ts, "type": "event_msg", "payload": payload}
        elif kind == 1:
            rec = {"timestamp": ts, "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "<environment_context>"
                     + _text(rng, 30, 90) + "</environment_context>"}]}}
        elif kind in (2, 5):
            phase = "commentary" if kind == 2 else "final_answer"
            text = _text(rng, 10, 150)
            if fmt == "legacy":
                payload = {"type": "agent_message", "message": text, "phase": phase}
            else:
                payload = {"type": "item_completed", "thread_id": "t", "turn_id": f"turn{i}",
                           "item": {"type": "AgentMessage", "id": f"a{i}", "phase": phase,
                                    "content": [{"type": "Text", "text": text}]}}
            rec = {"timestamp": ts, "type": "event_msg", "payload": payload}
        elif kind == 3:
            rec = {"timestamp": ts, "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": f"c{i}",
                "output": _text(rng, 150, 600)}}
        else:
            rec = {"timestamp": ts, "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"input_tokens": i}}}}
        out.append(json.dumps(rec))
    return out


def _codex_meta(session: str) -> str:
    return json.dumps({"timestamp": _stamp(0, 1), "type": "session_meta", "payload": {
        "id": session, "cwd": "/home/sam/projects/orchard", "originator": "codex_exec",
        "cli_version": "0.0.0"}})


def write_sources(root: Path, lines: int, fmt: str, seed: int) -> list[Path]:
    """The synthetic transcripts for one round; returns every file written."""
    rng = random.Random(seed)
    per = max(1, lines // SESSIONS)
    files: list[Path] = []
    claude_dir = root / "src" / "claude" / "home-sam-projects-orchard"
    claude_dir.mkdir(parents=True)
    codex_dir = root / "src" / "codex" / "2026" / "09" / "03"
    codex_dir.mkdir(parents=True)
    for s in range(SESSIONS):
        session = f"5a3e{s:04d}-0000-4000-8000-{s:012d}"
        path = claude_dir / f"{session}.jsonl"
        path.write_text("\n".join(claude_lines(rng, session, per, s * per, lines)) + "\n",
                        encoding="utf-8")
        files.append(path)
        rollout = codex_dir / f"rollout-2026-09-03T09-00-00-{session}.jsonl"
        body = [_codex_meta(session), *codex_lines(rng, fmt, per, s * per, lines)]
        rollout.write_text("\n".join(body) + "\n", encoding="utf-8")
        files.append(rollout)
    return files


def warm(files: list[Path]) -> None:
    """Open each file once: the first open of a new or changed file pays for the
    virus scanner (about 11 ms on Windows), which is no part of capture."""
    for path in files:
        with path.open("rb") as f:
            f.read(1)


def append_more(files: list[Path], lines: int, fmt: str, seed: int) -> None:
    rng = random.Random(seed + 1)
    extra = 6 * max(1, lines // SESSIONS // 300)  # about 2%, whole cycles of record kinds
    first = 6 * (lines // 6 + 1)
    for path in files:
        if path.name.startswith("rollout-"):
            more = codex_lines(rng, fmt, extra, first, lines)
        else:
            more = claude_lines(rng, path.stem, extra, first, lines)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(more) + "\n")


def child(args: argparse.Namespace) -> int:
    """One measurement in this process; prints a JSON line."""
    root = Path(os.environ["SMRITI_ROOT"])
    from smriti.daylog.collect import collect_once
    from smriti.daylog.config import DaylogConfig, SourceSpec

    files = write_sources(root, args.lines, args.codex, args.seed)
    cfg = DaylogConfig(root=root, sources=[
        SourceSpec(name="claude", kind="claude_jsonl",
                   glob=str(root / "src" / "claude" / "*" / "*.jsonl")),
        SourceSpec(name="codex", kind="codex_rollout",
                   glob=str(root / "src" / "codex" / "*" / "*" / "*" / "rollout-*.jsonl")),
    ])
    warm(files)
    t0 = time.perf_counter()
    first = collect_once(cfg)
    t1 = time.perf_counter()
    idle = collect_once(cfg)
    t2 = time.perf_counter()
    append_more(files, args.lines, args.codex, args.seed)
    warm(files)
    t3 = time.perf_counter()
    more = collect_once(cfg)
    t4 = time.perf_counter()
    print(json.dumps({"first_s": t1 - t0, "idle_s": t2 - t1, "incremental_s": t4 - t3,
                      "appended": [first.appended, idle.appended, more.appended]}))
    return 0


def git(argv: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def resolve(spec: str, work: Path) -> tuple[Path, str]:
    """A checkout directory as it is, or a git ref of this repository exported under ``work``."""
    path = Path(spec)
    if path.is_dir():
        try:
            label = git(["rev-parse", "--short", "HEAD"], path)
            if git(["status", "--porcelain", "--untracked-files=no"], path):
                label += "+changes"
        except (OSError, subprocess.CalledProcessError):
            label = path.name
        return path.resolve(), label
    sha = git(["rev-parse", "--verify", f"{spec}^{{commit}}"], REPO)
    dest = work / f"src-{sha[:12]}"
    if not dest.exists():
        data = subprocess.run(["git", "archive", "--format=tar", sha], cwd=REPO,
                              capture_output=True, check=True).stdout
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:  # Python before 3.11.4: git's own archive of this repository
                tar.extractall(dest)
    return dest, f"{spec} ({sha[:8]})"


def measure(checkout: Path, args: argparse.Namespace, work: Path) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="round-", dir=work))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "SMRITI_DAY_UTC_OFFSET", "SMRITI_EMBED_SERVICE")}
    env.update(PYTHONPATH=str(checkout / "src"), SMRITI_ROOT=str(root),
               SMRITI_EMBED_SERVICE="0", PYTHONDONTWRITEBYTECODE="1")
    if args.zone == "offset":
        env["SMRITI_DAY_UTC_OFFSET"] = "10"
    cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--child",
           "--lines", str(args.lines), "--codex", args.codex, "--seed", str(args.seed)]
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        if done.returncode != 0:
            raise RuntimeError(f"{checkout}: {done.stderr.strip()[-2000:]}")
        result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)  # synthetic data this run created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sides", nargs="*", help="checkout directories or git refs")
    parser.add_argument("--lines", type=int, default=20000,
                        help="lines per source (Claude and Codex each)")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--codex", choices=("legacy", "paginated"), default="legacy")
    parser.add_argument("--zone", choices=("offset", "system"), default="offset")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args)
    if not args.sides:
        parser.error("name at least one checkout or git ref")
    work = args.work or Path(tempfile.mkdtemp(prefix="smriti-bench-capture-"))
    work.mkdir(parents=True, exist_ok=True)
    sides = [resolve(spec, work) for spec in args.sides]
    runs: list[list[dict[str, Any]]] = [[] for _ in sides]
    for r in range(args.rounds):
        order = range(len(sides)) if r % 2 == 0 else reversed(range(len(sides)))
        for i in order:
            runs[i].append(measure(sides[i][0], args, work))
            print(f"round {r + 1}/{args.rounds} {sides[i][1]}: "
                  f"{runs[i][-1]['first_s'] * 1000:.0f} ms", file=sys.stderr)
    measures = ("first_s", "idle_s", "incremental_s")
    report: dict[str, Any] = {"lines": args.lines, "codex": args.codex, "zone": args.zone,
                              "rounds": args.rounds, "sides": []}
    print(f"\n{args.lines} lines per source ({args.codex} Codex), {args.rounds} rounds, "
          "median ms (min-max)")
    print(f"{'side':34} {'first pass':>22} {'idle pass':>18} {'+2% lines':>20}  appended")
    base: dict[str, float] = {}
    for (path, label), rows in zip(sides, runs, strict=True):
        cells: list[str] = []
        entry: dict[str, Any] = {"side": label, "path": str(path),
                                 "appended": rows[0]["appended"]}
        for m in measures:
            values = [row[m] * 1000 for row in rows]
            med = statistics.median(values)
            entry[m] = {"median_ms": med, "min_ms": min(values), "max_ms": max(values)}
            ratio = f" x{med / base[m]:.2f}" if base.get(m) else ""
            base.setdefault(m, med)
            cells.append(f"{med:.1f} ({min(values):.0f}-{max(values):.0f}){ratio}")
        report["sides"].append(entry)
        print(f"{label[:34]:34} {cells[0]:>22} {cells[1]:>18} {cells[2]:>20}  "
              f"{rows[0]['appended']}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

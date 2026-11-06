"""Time the nightly's steps that call no model, on copies of a real tree, for one or more checkouts.

    python scripts/bench_nightly.py --tree DIR A [B ...] [--rounds R] [--work DIR]
                                    [--json FILE] [--schedule] [--nights N]

Each side is a checkout directory or a git ref of this repository (exported
with ``git archive`` into ``--work``). Each measurement gets a fresh copy of
what the nightly reads in ``--tree`` (``log/``, ``.smriti/daylog.json`` and
``heartbeat/voice-transcripts/``) and runs in a fresh process with
``PYTHONPATH=<side>/src`` (and its reference profile), ``-B``,
``SMRITI_ROOT`` set to the copy, a runtime directory of its own and the
embedding service off. The tree given is only read. Sides alternate (A, B,
B, A, ...) so drift on the machine falls on all of them. Every file of the
copy is read once before anything is timed: the first open of a new file
pays for the virus scanner, which a nightly over an old tree doesn't.

What a round times, in that process:

- ``reconcile``: one ``collect_once`` with the copy's capture config. Its
  sources are the transcripts it names (read, never written); what is new
  in them since the copy's state goes into the copy's day-log.
- ``stale days``: the scan for days to render and digest (every day-file's
  hash against its render's and digest's).
- ``render``: the three most recent days and the largest, as the nightly
  renders a day it digests (``render_snapshot`` where a side has it).
- ``rollup scan``: the scan for closed weeks, months and years to roll up.
- ``backup status``: reading the last backup commit and push from a
  synthetic, full-size backup log (sides without it show nothing; the
  nightly below reads it too on a side that has it).
- ``nightly, no model``: ``run_nightly`` with both caps at 0 and no index
  or witness: reconcile (nothing new by then), the scans, the renders of
  stale days, the status file.

``--schedule`` instead runs ``--nights`` nights (default 3, a day apart) on
one copy per side with a stub model, capture off (each side starts from the
same day-log) and the default caps, and prints what each night digested and
rolled up, which must agree between sides: hardening H12.12 must schedule no
digest and no rollup that the code before it wouldn't.
"""

from __future__ import annotations

import argparse
import io
import json
import os
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
COPIED = ("log", ".smriti/daylog.json", "heartbeat/voice-transcripts")
MEASURES = (
    ("reconcile_ms", "reconcile"),
    ("stale_days_ms", "stale days"),
    ("render_ms", "render, 3 recent + largest"),
    ("rollup_scan_ms", "rollup scan"),
    ("backup_status_ms", "backup status"),
    ("nightly_ms", "nightly, no model"),
)


# ── in the child process ─────────────────────────────────────────────


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _days(cfg: Any) -> list[tuple[str, int]]:
    """Every day-file of the copy, with its size."""
    out = []
    for path in cfg.log_dir.glob("[0-9]" * 4 + "/[0-9][0-9]/[0-9][0-9].jsonl"):
        out.append((f"{path.parent.parent.name}-{path.parent.name}-{path.stem}",
                    path.stat().st_size))
    return sorted(out)


def synthetic_backup_log(root: Path, lines: int = 1000) -> None:
    """A backup log as full as the script keeps it, ending in a run of failed pushes."""
    git = root / ".git"
    git.mkdir(exist_ok=True)
    (git / "config").write_text('[remote "origin"]\n\turl = /nowhere/memory.git\n',
                                encoding="utf-8")
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with (git / "smriti-backup.jsonl").open("w", encoding="utf-8") as f:
        for i in range(lines):
            at = (start + timedelta(hours=i)).isoformat(timespec="seconds")
            if i % 2 == 0:
                rec: dict[str, Any] = {"at": at, "event": "commit", "tag": "hourly", "ok": True,
                                       "code": 0, "out": ""}
            else:
                failing = i > lines - 200
                rec = {"at": at, "event": "push", "tag": "hourly", "ok": not failing,
                       "code": 128 if failing else 0,
                       "to": "origin refs/heads/main:refs/heads/main",
                       "out": "fatal: unable to access: Could not resolve host" if failing else ""}
            f.write(json.dumps(rec) + "\n")


def warm(root: Path) -> None:
    """Read every file once: the first open of a new file pays for the virus scanner
    (on Windows), which a nightly over an old tree doesn't."""
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as f:
                while f.read(1 << 20):
                    pass


def child_time(root: Path) -> dict[str, Any]:
    from smriti.daylog import nightly
    from smriti.daylog.collect import collect_once
    from smriti.daylog.config import load_config
    from smriti.daylog.model import day_tz
    from smriti.daylog.rollup import enumerate_stale_periods
    from smriti.daylog.state import DaylogState

    synthetic_backup_log(root)
    warm(root)
    cfg = load_config(root)
    now = datetime.now(tz=day_tz(root))
    target = nightly._target_day(now, root)
    out: dict[str, Any] = {}

    t0 = time.perf_counter()
    report = collect_once(cfg)
    out["reconcile_ms"] = _ms(t0)
    out["appended"] = report.appended

    dirty = DaylogState.load(cfg.state_path).dirty_days
    t0 = time.perf_counter()
    stale = nightly._stale_days(cfg, report.changed_days, dirty, target)
    out["stale_days_ms"] = _ms(t0)
    out["stale"] = len(stale)

    days = _days(cfg)
    chosen = [d for d, _ in days[-3:]] + [max(days, key=lambda d: d[1])[0]]
    try:
        from smriti.daylog.render import render_snapshot as render
    except ImportError:
        from smriti.daylog.render import render_day as render
    t0 = time.perf_counter()
    for day in chosen:
        render(cfg, day)
    out["render_ms"] = _ms(t0)
    out["rendered"] = chosen

    t0 = time.perf_counter()
    periods = enumerate_stale_periods(cfg, target)
    out["rollup_scan_ms"] = _ms(t0)
    out["stale_periods"] = len(periods)

    try:
        from smriti.backup import backup_status
    except ImportError:
        out["backup_status_ms"] = None
    else:
        t0 = time.perf_counter()
        backup_status(root)
        out["backup_status_ms"] = _ms(t0)

    t0 = time.perf_counter()
    status = nightly.run_nightly(cfg, now=now, rollup_cap=0, digest_cap=0, reindex=False,
                                 witness=False)
    out["nightly_ms"] = _ms(t0)
    out["nightly_ok"] = status.get("ok")
    return out


def child_schedule(root: Path, nights: int) -> dict[str, Any]:
    """What each night digests and rolls up with a stub model and capture off."""
    from smriti.daylog import collect, nightly
    from smriti.daylog import digest as digest_mod
    from smriti.daylog.collect import CollectReport
    from smriti.daylog.config import load_config
    from smriti.daylog.model import day_tz
    from smriti.llm.types import LLMResponse

    calls: list[str] = []
    built: list[str] = []

    class Stub:
        def is_available(self) -> bool:
            return True

        def call(self, request: Any) -> Any:
            calls.append(request.user.split("\n", 1)[0][:60])
            return LLMResponse(text="## Highlights\n- a stub digest")

    due: list[str] = []
    real_build, real_scan = nightly.build_rollup, nightly.enumerate_stale_periods

    def build(period: Any, attempts: Any, cfg: Any = None) -> Path:
        built.append(period.out_path.relative_to(root / "log").as_posix())
        path: Path = real_build(period, attempts, cfg)
        return path

    def scan(cfg: Any, today: Any) -> list[Any]:
        periods: list[Any] = real_scan(cfg, today)
        due.extend(p.out_path.relative_to(root / "log").as_posix() for p in periods)
        return periods

    digest_mod.get_provider = lambda name=None: Stub()
    collect.collect_once = lambda cfg, **kw: CollectReport()
    nightly.build_rollup = build
    nightly.enumerate_stale_periods = scan

    cfg = load_config(root)
    first = datetime.now(tz=day_tz(root)).replace(hour=3, minute=5, second=0, microsecond=0)
    out: list[dict[str, Any]] = []
    for n in range(nights):
        now = first + timedelta(days=n)
        calls.clear()
        built.clear()
        due.clear()
        status = nightly.run_nightly(cfg, now=now, reindex=False, witness=False)
        steps = status["steps"]
        assert isinstance(steps, dict)
        out.append({
            "target": status["target_day"],
            "digested": steps["digest"]["days"],
            "deferred": steps["digest"]["deferred"],
            "rendered": len(steps["render"]["days"]),
            "rollups_due": list(due),
            "rolled_up": list(built),
            "rollups_remaining": steps["rollups"]["remaining"],
            "model_calls": len(calls),
        })
    return {"nights": out}


def child(args: argparse.Namespace) -> int:
    root = Path(os.environ["SMRITI_ROOT"])
    result = child_schedule(root, args.nights) if args.schedule else child_time(root)
    print(json.dumps(result))
    return 0


# ── the parent ───────────────────────────────────────────────────────


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


def copy_tree(tree: Path, dest: Path) -> None:
    """What the nightly reads of *tree*, copied to the new directory *dest*."""
    for rel in COPIED:
        src = tree / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel)
        elif src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)


def measure(checkout: Path, tree: Path, args: argparse.Namespace, work: Path) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="round-", dir=work))
    copy_tree(tree, root)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SMRITI_", "NARADA_")) and k != "PYTHONPATH"}
    paths = [checkout / "src"]
    if (checkout / "profiles" / "reference" / "src").is_dir():
        paths.append(checkout / "profiles" / "reference" / "src")
    env.update(PYTHONPATH=os.pathsep.join(map(str, paths)), SMRITI_ROOT=str(root),
               NARADA_ROOT=str(root), SMRITI_RUNTIME_DIR=str(root.with_name(root.name + "-run")),
               SMRITI_EMBED_SERVICE="0", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--child",
           "--nights", str(args.nights)] + (["--schedule"] if args.schedule else [])
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600,
                              encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise RuntimeError(f"{checkout}: {done.stderr.strip()[-3000:]}")
        result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)  # the copy this round made
        shutil.rmtree(root.with_name(root.name + "-run"), ignore_errors=True)


def _cell(values: list[float], base: float | None) -> str:
    med = statistics.median(values)
    ratio = f" x{med / base:.2f}" if base else ""
    return f"{med:.0f} ({min(values):.0f}-{max(values):.0f}){ratio}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sides", nargs="*", help="checkout directories or git refs")
    parser.add_argument("--tree", type=Path, help="the memory tree to copy from (only read)")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--schedule", action="store_true",
                        help="print what each side's nights digest and roll up, instead")
    parser.add_argument("--nights", type=int, default=3)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(args)
    if not args.sides or args.tree is None:
        parser.error("name the tree (--tree) and at least one checkout or git ref")
    tree = args.tree.resolve()
    if not (tree / "log").is_dir():
        parser.error(f"{tree} has no log/ to copy")
    work = (args.work or Path(tempfile.mkdtemp(prefix="smriti-bench-nightly-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    sides = [resolve(spec, work) for spec in args.sides]

    if args.schedule:
        plans = [measure(path, tree, args, work) for path, _label in sides]
        for (_path, label), plan in zip(sides, plans, strict=True):
            print(f"\n{label}:")
            for night in plan["nights"]:
                print("  " + json.dumps(night))
        same = all(plan == plans[0] for plan in plans)
        print("\nschedules " + ("agree" if same else "DIFFER"))
        if args.json:
            args.json.write_text(json.dumps({"sides": [s[1] for s in sides], "plans": plans},
                                            indent=1), encoding="utf-8")
        return 0 if same else 1

    runs: list[list[dict[str, Any]]] = [[] for _ in sides]
    for r in range(args.rounds):
        order = range(len(sides)) if r % 2 == 0 else reversed(range(len(sides)))
        for i in order:
            runs[i].append(measure(sides[i][0], tree, args, work))
            print(f"round {r + 1}/{args.rounds} {sides[i][1]}: nightly "
                  f"{runs[i][-1]['nightly_ms']:.0f} ms", file=sys.stderr)
    print(f"\n{args.rounds} rounds, median ms (min-max), ratio to the first side")
    print(f"{'measure':28}" + "".join(f" {label[:30]:>30}" for _p, label in sides))
    for key, name in MEASURES:
        base: float | None = None
        cells = []
        for rows in runs:
            values = [row[key] for row in rows if row.get(key) is not None]
            if not values:
                cells.append("-")
                continue
            cells.append(_cell(values, base))
            base = base or statistics.median(values)
        print(f"{name:28}" + "".join(f" {c:>30}" for c in cells))
    for (_p, label), rows in zip(sides, runs, strict=True):
        print(f"{label}: appended {[r['appended'] for r in rows]}, stale days "
              f"{rows[0]['stale']}, stale periods {rows[0]['stale_periods']}, "
              f"rendered {rows[0]['rendered']}")
    if args.json:
        args.json.write_text(json.dumps({"sides": [s[1] for s in sides], "runs": runs},
                                        indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

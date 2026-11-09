"""Benchmark two smriti checkouts on copies of one memory tree.

    python scripts/bench.py A B [--tree DIR] [--queries FILE] [--rounds N]
                            [--work DIR] [--wake-cwd DIR] [--json FILE] [--fail-slower]

``A`` and ``B`` are checkout directories, or git refs of this repository
(exported with ``git archive`` into the work directory). Each side gets
its own fresh copy of the tree and its own runtime directory, and every
measure runs in fresh processes with ``PYTHONPATH=<checkout>/src`` (plus
``<checkout>/profiles/reference/src`` when there is one), ``SMRITI_ROOT``
set to that side's copy, and ``SMRITI_RUNTIME_DIR`` to its runtime
directory. So each side runs its own code on its own copy, and the tree
given is only ever read.

What is measured, per round: MCP startup and ``initialize``; ``tools/list``,
first and warm; the first ``smriti_read`` and the time until a read gets
vector results; warm reads (p50 and p90 over three passes of the queries);
``smriti_write`` (p50 and max of five); the wake hook (median of three:
``python -P -m smriti.wake``, or, for a checkout from before the wake ran
from the package, a copy of its ``narada/.smriti/wake.py`` placed in the
tree copy's ``.smriti/``, never run from the checkout, since it touches a
file beside itself); ``smriti status``; a CLI ``smriti read``; ``smriti index``
with nothing changed and with five new files; the MCP server's memory and
the embedding service's, when one runs (with psutil installed). Any
embedding service a round starts is stopped at its end, so each round
starts cold.

Each side first gets one unmeasured pass of the short processes, which
compiles its bytecode and fills its new runtime directory (the profile
lookup's cache), as an installation in use has them. Rounds then alternate
(A, B, B, A, ...) so drift on the machine falls on both sides. The table
gives each side's mean over its rounds and B's ratio to A, then a top-5
parity check per query (as lists, since a result can repeat a file).
``--fail-slower`` exits 1 when any time in B is more than 10% above A's.

Queries come from ``--queries`` (one per line; blank lines and ``#``
comments skipped), else a short generic list. Without ``--tree`` the
packaged synthetic corpus (``src/smriti/eval/fixture``) is used, indexed in
each copy by that side's own ``smriti index``.

Links in the tree are copied as links (robocopy ``/SJ /SL`` on Windows), so
whatever they point to is shared with the original. The bench writes only
inside the copies (a ``bench/`` branch and ``.smriti/``), and it never
deletes anything: each run's copies stay in a new directory under
``--work`` until you remove them.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # memory is then left blank
    psutil = None

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
SLOWER = 1.10  # B is slower when its time is more than this times A's
CALL_TIMEOUT_S = 300.0  # an MCP call, or a command, that takes longer is a failure
UNTIL_VECTORS_S = 180.0  # how long to keep reading for vector results
DEFAULT_QUERIES = (
    "who is the agent",
    "what was decided about storage",
    "notes on rest and recovery",
    "the lantern project",
    "supplies to buy",
    "a change of role",
    "what the user does for work",
    "beliefs about evidence and experiments",
)
# "[1] notes/storage design.md :: Heading (score: 0.91, depth: 2)": the source runs up to
# the heading or the score, so a path with spaces stays whole.
RESULT_RE = re.compile(r"^\[(\d+)\] (.+?)(?: :: .*?)? \(score: -?[\d.]+, depth: -?\d+\)\s*$",
                       re.M)
KEYWORD_ONLY = "keyword-only"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# (key, what the table calls it, unit)
MEASURES: tuple[tuple[str, str, str], ...] = (
    ("mcp_initialize_ms", "MCP server: start and initialize", "ms"),
    ("tools_list_first_ms", "tools/list, first", "ms"),
    ("tools_list_warm_ms", "tools/list, warm", "ms"),
    ("read_first_ms", "smriti_read: first query", "ms"),
    ("read_until_vectors_ms", "smriti_read: until vector results", "ms"),
    ("read_warm_p50_ms", "smriti_read: warm p50", "ms"),
    ("read_warm_p90_ms", "smriti_read: warm p90", "ms"),
    ("write_p50_ms", "smriti_write: p50", "ms"),
    ("write_max_ms", "smriti_write: max", "ms"),
    ("wake_ms", "wake hook (median of 3)", "ms"),
    ("status_ms", "`smriti status`", "ms"),
    ("cli_read_ms", "CLI `smriti read` (median of 3)", "ms"),
    ("index_nochange_ms", "`smriti index`, nothing changed", "ms"),
    ("index_5files_ms", "`smriti index`, 5 new files", "ms"),
    ("mcp_rss_mb", "MCP server memory", "MB"),
    ("service_rss_mb", "embedding service memory", "MB"),
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ── Sides ────────────────────────────────────────────────────────────


@dataclass
class Side:
    name: str  # "A" or "B"
    label: str  # what the table calls it
    checkout: Path
    tree: Path  # this side's copy of the tree
    runtime: Path  # its own runtime directory: key, services, caches
    rounds: list[dict[str, Any]] = field(default_factory=list)

    def env(self, **extra: str) -> dict[str, str]:
        """The environment its processes get: its code, its copy, its runtime directory."""
        return side_env(os.environ, self.checkout, self.tree, self.runtime, **extra)

    @property
    def has_service(self) -> bool:
        return (self.checkout / "src" / "smriti" / "embed" / "service.py").is_file()


def side_env(base: Mapping[str, str], checkout: Path, tree: Path, runtime: Path,
             **extra: str) -> dict[str, str]:
    env = {k: v for k, v in base.items()
           if not k.startswith(("SMRITI_", "NARADA_")) and k != "PYTHONPATH"}
    paths = [checkout / "src"]
    if (checkout / "profiles" / "reference" / "src").is_dir():
        paths.append(checkout / "profiles" / "reference" / "src")
    env.update(PYTHONPATH=os.pathsep.join(str(p) for p in paths), SMRITI_ROOT=str(tree),
               NARADA_ROOT=str(tree), SMRITI_RUNTIME_DIR=str(runtime),
               PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def git(args: Sequence[str], cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True)
    return done.stdout.strip()


def resolve_checkout(spec: str, work: Path) -> tuple[Path, str]:
    """A checkout directory as it is, or a git ref of this repository exported to ``work``."""
    path = Path(spec)
    if path.is_dir():
        path = path.resolve()
        try:
            label = git(["rev-parse", "--short", "HEAD"], path)
            if git(["status", "--porcelain", "--untracked-files=no"], path):
                label += "+changes"
        except (OSError, subprocess.CalledProcessError):
            label = path.name
        return path, label
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


def _is_link(path: Path) -> bool:
    """A symlink or, on Windows, a junction (any reparse point)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0)
                                             & FILE_ATTRIBUTE_REPARSE_POINT)


def check_writable(copy: Path) -> None:
    """Refuse a copy whose written paths are links, so no write escapes into another tree.

    The bench writes the root's ``index.md`` (index upkeep), everything under
    ``.smriti/`` (index, metrics, the wake's ``last-activity``, an older
    checkout's wake script) and ``bench/``. A link there,
    kept as a link by the copy, would write through to wherever it points.
    """
    bad = [p for p in (copy, copy / "index.md") if _is_link(p)]
    for base in (copy / ".smriti", copy / "bench"):
        stack = [base]
        while stack:
            path = stack.pop()
            if not os.path.lexists(path):
                continue
            if _is_link(path):
                bad.append(path)
            elif path.is_dir():
                stack.extend(path.iterdir())
    if bad:
        raise SystemExit(f"bench: {copy} would be written through links: "
                         f"{', '.join(str(b) for b in bad[:3])}; copy a tree without them")


def copy_tree(src: Path, dst: Path) -> None:
    """A copy of ``src`` at ``dst`` (a new directory), without .git, links kept as links."""
    if sys.platform == "win32":
        done = subprocess.run(["robocopy", str(src), str(dst), "/MIR", "/XD", ".git", "/SJ",
                               "/SL", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/MT:16"],
                              capture_output=True, text=True, errors="replace")
        if done.returncode >= 8:  # robocopy: 0-7 are kinds of success
            raise RuntimeError(f"robocopy failed ({done.returncode}): {done.stdout[-500:]}")
    else:
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def load_queries(path: Path | None) -> list[str]:
    if path is None:
        return list(DEFAULT_QUERIES)
    lines = path.read_text(encoding="utf-8").splitlines()
    queries = [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not queries:
        raise SystemExit(f"no queries in {path}")
    return queries


# ── Measuring ────────────────────────────────────────────────────────


def ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def pct(xs: Sequence[float], q: float) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def run(side: Side, args: Sequence[str], *, cwd: Path | None = None,
        **extra: str) -> tuple[float, subprocess.CompletedProcess[str]]:
    t0 = time.perf_counter()
    done = subprocess.run([PY, *args], env=side.env(**extra), cwd=cwd or side.tree,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=CALL_TIMEOUT_S)
    return ms_since(t0), done


class Mcp:
    """``python -m smriti.mcp_server`` for one side, spoken to over stdio."""

    def __init__(self, side: Side) -> None:
        self.started = time.perf_counter()
        self.proc = subprocess.Popen([PY, "-m", "smriti.mcp_server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=side.env(), cwd=side.tree, text=True,
                                     encoding="utf-8", errors="replace")
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.n = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def call(self, method: str, params: dict[str, Any] | None = None, *,
             since: float | None = None) -> tuple[float, dict[str, Any]]:
        """The answer to one request, and how long it took (from ``since``, if given)."""
        assert self.proc.stdin is not None
        self.n += 1
        t0 = time.perf_counter() if since is None else since
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.perf_counter() + CALL_TIMEOUT_S
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.perf_counter()))
            except queue.Empty:
                raise RuntimeError(f"no answer to {method} in {CALL_TIMEOUT_S:.0f}s") from None
            if line is None:
                raise RuntimeError(f"the MCP server exited during {method}")
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("id") == self.n:
                return ms_since(t0), data

    def tool(self, name: str, arguments: dict[str, Any]) -> tuple[float, str]:
        """The tool's text; raises when the call failed, so no failure is timed as a result."""
        ms, data = self.call("tools/call", {"name": name, "arguments": arguments})
        if "error" in data:
            raise RuntimeError(f"{name}: JSON-RPC error {json.dumps(data['error'])[:300]}")
        result = data.get("result") or {}
        content = result.get("content") or []
        text = str(content[0].get("text", "")) if content else ""
        if result.get("isError") or not content:
            raise RuntimeError(f"{name} failed: {(text or json.dumps(data))[:300]}")
        return ms, text

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)


def rss_mb(pid: int) -> float | None:
    if psutil is None:
        return None
    try:
        return float(psutil.Process(pid).memory_info().rss) / 2**20
    except psutil.Error:
        return None


def services(side: Side) -> list[dict[str, Any]]:
    """The discovery records in this side's runtime directory: services the bench started."""
    found = []
    for path in sorted(side.runtime.glob("embed-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            found.append(data)
    return found


def stop_services(side: Side) -> None:
    """Stop every embedding service this side's rounds started; wait for them to go."""
    if not side.has_service or not services(side):
        return
    run(side, ["-m", "smriti", "embed", "stop", "--root", str(side.tree)])
    deadline = time.monotonic() + 60
    while services(side) and time.monotonic() < deadline:
        time.sleep(0.25)
    for left in services(side):  # its discovery file outlived the stop
        pid = int(left["pid"])
        if psutil is not None and psutil.pid_exists(pid):
            log(f"  terminating embedding service pid {pid}, which did not stop")
            try:
                psutil.Process(pid).terminate()
            except psutil.Error as exc:
                log(f"  could not terminate pid {pid}: {exc}")
        else:
            log(f"  an embedding service record remains (pid {pid}); check it is gone")


def top5(text: str) -> list[str]:
    sources = [source for _, source in RESULT_RE.findall(text)][:5]
    return ([f"<{KEYWORD_ONLY}>"] if text.startswith(KEYWORD_ONLY) else []) + sources


def measure_mcp(side: Side, queries: Sequence[str], out: dict[str, Any]) -> None:
    mcp = Mcp(side)
    try:
        out["mcp_initialize_ms"], _ = mcp.call(
            "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "smriti-bench", "version": "1"}},
            since=mcp.started)
        out["tools_list_first_ms"], listed = mcp.call("tools/list")
        out["tools_list_warm_ms"], _ = mcp.call("tools/list")
        out["tools"] = len((listed.get("result") or {}).get("tools") or [])

        t0 = time.perf_counter()
        out["read_first_ms"], text = mcp.tool("smriti_read", {"query": queries[0], "top_k": 5})
        out["read_first_keyword_only"] = text.startswith(KEYWORD_ONLY)
        while text.startswith(KEYWORD_ONLY) and ms_since(t0) < UNTIL_VECTORS_S * 1000:
            time.sleep(0.5)
            _, text = mcp.tool("smriti_read", {"query": queries[0], "top_k": 5})
        out["read_until_vectors_ms"] = ms_since(t0)
        out["read_vectors_reached"] = not text.startswith(KEYWORD_ONLY)

        warm: list[float] = []
        tops: dict[str, list[str]] = {}
        for _ in range(3):
            for q in queries:
                ms, text = mcp.tool("smriti_read", {"query": q, "top_k": 5})
                warm.append(ms)
                tops[q] = top5(text)  # the last pass's
        out["read_warm_p50_ms"], out["read_warm_p90_ms"] = statistics.median(warm), pct(warm, 0.9)
        out["top5"] = tops

        writes: list[float] = []
        for i in range(5):
            ms, text = mcp.tool("smriti_write", {
                "content": f"Benchmark entry {i}: timing a write.", "branch": "bench/perf",
                "title": f"bench {i}"})
            writes.append(ms)
            if not text.startswith("Written"):
                out.setdefault("problems", []).append(f"smriti_write: {text[:200]}")
        out["write_p50_ms"], out["write_max_ms"] = statistics.median(writes), max(writes)
        out["writes_ms"] = writes
        out["mcp_rss_mb"] = rss_mb(mcp.proc.pid)
        running = services(side)
        out["service_rss_mb"] = rss_mb(int(running[0]["pid"])) if running else None
    finally:
        mcp.close()


def place_wake(side: Side) -> list[str] | None:
    """The arguments that run the checkout's wake hook; None if it has none.

    ``-P -m smriti.wake``, as the hook runs it. A checkout from before the
    wake ran from the package has a ``narada/.smriti/wake.py`` instead: it is
    copied into the tree copy's .smriti/ and run from there, never from the
    checkout, since the script touches ``last-activity`` beside itself.
    """
    if (side.checkout / "src" / "smriti" / "wake" / "__main__.py").is_file():
        return ["-P", "-m", "smriti.wake"]
    source = side.checkout / "narada" / ".smriti" / "wake.py"
    if not source.is_file():
        return None
    wake = side.tree / ".smriti" / "wake.py"
    wake.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, wake)
    return [str(wake)]


def measure_cli(side: Side, queries: Sequence[str], round_no: int, wake_cwd: Path,
                out: dict[str, Any]) -> None:
    problems: list[str] = out.setdefault("problems", [])

    def checked(name: str, done: subprocess.CompletedProcess[str]) -> None:
        if done.returncode != 0:
            problems.append(f"{name} exited {done.returncode}: {(done.stderr or '')[-300:]}")

    wake = place_wake(side)
    if wake is not None:
        wakes = []
        for _ in range(3):
            ms, done = run(side, wake, cwd=wake_cwd, SMRITI_WAKE="1")
            wakes.append(ms)
            checked("wake", done)
        out["wake_ms"] = statistics.median(wakes)
        out["wake_chars"] = len(done.stdout)
        out["wake_headers"] = [ln[:60] for ln in done.stdout.splitlines() if ln.startswith("--- ")]

    out["status_ms"], done = run(side, ["-m", "smriti", "status"])
    checked("smriti status", done)

    reads = []
    for q in list(queries[1:4]) or list(queries[:1]):
        ms, done = run(side, ["-m", "smriti", "read", q])
        reads.append(ms)
        checked("smriti read", done)
    out["cli_read_ms"] = statistics.median(reads)

    out["index_nochange_ms"], done = run(side, ["-m", "smriti", "index"])
    checked("smriti index", done)
    touched = side.tree / "bench" / "touched"
    touched.mkdir(parents=True, exist_ok=True)
    for i in range(5):  # new each round, and the same on both sides
        (touched / f"note-r{round_no}-{i}.md").write_text(
            f"# Note {round_no}.{i}\n\nA benchmark note about {queries[i % len(queries)]}.\n",
            encoding="utf-8")
    out["index_5files_ms"], done = run(side, ["-m", "smriti", "index"])
    checked("smriti index (5 new files)", done)


def one_round(side: Side, queries: Sequence[str], wake_cwd: Path) -> dict[str, Any]:
    round_no = len(side.rounds) + 1
    log(f"round {round_no} of {side.name} ({side.label})")
    out: dict[str, Any] = {"side": side.name, "round": round_no}
    try:
        measure_mcp(side, queries, out)
        measure_cli(side, queries, round_no, wake_cwd, out)
    finally:
        stop_services(side)
    side.rounds.append(out)
    for problem in out.get("problems", []):
        log(f"  problem: {problem}")
    return out


def warm_up(side: Side, wake_cwd: Path) -> None:
    """One unmeasured pass of the short processes: an MCP server's start and tools
    list, the wake and status. It compiles the checkout's bytecode and fills the
    side's new runtime directory (the profile lookup's cache), as a long-used
    installation has them, so neither falls on whichever round comes first."""
    mcp = Mcp(side)
    try:
        mcp.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "smriti-bench", "version": "1"}})
        mcp.call("tools/list")
    finally:
        mcp.close()
    wake = place_wake(side)
    if wake is not None:
        run(side, wake, cwd=wake_cwd, SMRITI_WAKE="1")
    run(side, ["-m", "smriti", "status"])


def prepare(side: Side, source: Path, wake_cwd: Path) -> None:
    log(f"copying {source} for {side.name}")
    copy_tree(source, side.tree)
    check_writable(side.tree)
    side.runtime.mkdir(parents=True, exist_ok=True)
    _, done = run(side, ["-c", "import smriti; print(smriti.__file__)"])
    where = Path(done.stdout.strip() or ".").resolve()
    if done.returncode != 0 or (side.checkout / "src") not in where.parents:
        raise SystemExit(f"{side.name} imports smriti from {where}, not from {side.checkout}: "
                         f"{done.stderr[-300:]}")
    if not (side.tree / ".smriti" / "index.db").exists():
        log(f"building {side.name}'s index with its own `smriti index`")
        ms, done = run(side, ["-m", "smriti", "index"])
        if done.returncode != 0:
            raise SystemExit(f"{side.name}: `smriti index` failed: {done.stderr[-500:]}")
        log(f"  built in {ms / 1000:.1f} s")
    warm_up(side, wake_cwd)


# ── Reporting ────────────────────────────────────────────────────────


def mean_of(rounds: Sequence[dict[str, Any]], key: str) -> float | None:
    values = [r[key] for r in rounds
              if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
    return statistics.mean(values) if values else None


def cell(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "MB":
        return f"{value:.0f} MB"
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def summarise(a: Sequence[dict[str, Any]],
              b: Sequence[dict[str, Any]]) -> list[tuple[str, str, float | None, float | None]]:
    """(key, unit, A's mean, B's mean) for every measure."""
    return [(key, unit, mean_of(a, key), mean_of(b, key)) for key, _, unit in MEASURES]


def slower(rows: Sequence[tuple[str, str, float | None, float | None]],
           threshold: float = SLOWER) -> list[str]:
    """The time measures in which B is more than ``threshold`` times A."""
    return [key for key, unit, va, vb in rows
            if unit == "ms" and va and vb is not None and vb > va * threshold]


def parity(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Per query, how B's top 5 compares with A's, as lists (a file can recur)."""
    verdicts = []
    for q, top_a in a.items():
        top_b = b.get(q, [])
        if top_a == top_b:
            verdict = "same"
        elif sorted(top_a) == sorted(top_b):
            verdict = "same results, another order"
        else:
            shared = sum(min(top_a.count(s), top_b.count(s)) for s in set(top_a))
            verdict = f"differs: {shared}/{max(len(top_a), len(top_b))} shared"
        verdicts.append((q, verdict))
    return verdicts


def report(a: Side, b: Side, threshold: float = SLOWER) -> tuple[str, list[str]]:
    names = {key: name for key, name, _ in MEASURES}
    rows = summarise(a.rounds, b.rounds)
    lines = [f"| Measure | A: {a.label} | B: {b.label} | B/A |", "|---|---|---|---|"]
    late = set(slower(rows, threshold))
    for key, unit, va, vb in rows:
        ratio = f"{vb / va:.2f}" if va and vb is not None else "-"
        if key in late:
            ratio += " (slower)"
        lines.append(f"| {names[key]} | {cell(va, unit)} | {cell(vb, unit)} | {ratio} |")
    lines.append("")
    lines.append(f"Rounds: {len(a.rounds)} each, alternating. First query keyword-only: "
                 f"A {[r.get('read_first_keyword_only') for r in a.rounds]}, "
                 f"B {[r.get('read_first_keyword_only') for r in b.rounds]}. "
                 f"Tools: A {[r.get('tools') for r in a.rounds]}, "
                 f"B {[r.get('tools') for r in b.rounds]}.")
    if a.rounds and b.rounds:
        wake_a, wake_b = a.rounds[-1].get("wake_headers"), b.rounds[-1].get("wake_headers")
        lines.append(f"Wake sections the same: {wake_a == wake_b} "
                     f"({a.rounds[-1].get('wake_chars')} and "
                     f"{b.rounds[-1].get('wake_chars')} characters).")
        lines.append("")
        lines.append("Top-5 parity (each side's last round):")
        for q, verdict in parity(a.rounds[-1].get("top5", {}), b.rounds[-1].get("top5", {})):
            lines.append(f"- {q[:50]}: {verdict}")
    problems = [f"{r['side']}{r['round']}: {p}" for side in (a, b) for r in side.rounds
                for p in r.get("problems", [])]
    if problems:
        lines.append("")
        lines.append("Problems (a failed step can look fast):")
        lines.extend(f"- {p}" for p in problems)
    return "\n".join(lines), sorted(late)


def order(rounds: int) -> Iterator[int]:
    """Which side goes when: 0, 1, 1, 0, 0, 1, ... (A, B, B, A, ...)."""
    for r in range(rounds):
        yield from ((0, 1) if r % 2 == 0 else (1, 0))


@contextmanager
def stopping(sides: Sequence[Side]) -> Iterator[None]:
    try:
        yield
    finally:
        for side in sides:
            stop_services(side)


def main(argv: Sequence[str] | None = None,
         measure: Callable[[Side, Sequence[str], Path], dict[str, Any]] = one_round) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("a", help="the baseline: a checkout directory, or a git ref")
    parser.add_argument("b", help="the candidate: a checkout directory, or a git ref")
    parser.add_argument("--tree", type=Path,
                        help="the memory tree to copy (default: the synthetic eval corpus)")
    parser.add_argument("--queries", type=Path, help="queries, one per line")
    parser.add_argument("--rounds", type=int, default=2, help="rounds per side (default 2)")
    parser.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "smriti-bench",
                        help="where each run's copies go (never deleted)")
    parser.add_argument("--wake-cwd", type=Path, default=REPO,
                        help="the wake hook's current directory (default: this repository)")
    parser.add_argument("--json", type=Path, help="write every round's numbers here")
    parser.add_argument("--fail-slower", action="store_true",
                        help="exit 1 when a time in B is more than 10%% above A's")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")

    queries = load_queries(args.queries)
    work = (args.work / time.strftime("%Y%m%d-%H%M%S")).resolve()
    work.mkdir(parents=True)
    sides = []
    for name, spec in (("A", args.a), ("B", args.b)):
        checkout, label = resolve_checkout(spec, args.work.resolve())
        sides.append(Side(name, label, checkout, work / f"tree-{name}", work / f"run-{name}"))
    if args.tree is not None:
        source = args.tree.resolve()
    else:
        fixture = sides[1].checkout / "src" / "smriti" / "eval" / "fixture"
        source = fixture if fixture.is_dir() else REPO / "src" / "smriti" / "eval" / "fixture"
    log(f"work directory: {work}")
    with stopping(sides):
        for side in sides:
            prepare(side, source, args.wake_cwd.resolve())
        for i in order(args.rounds):
            measure(sides[i], queries, args.wake_cwd.resolve())

    table, late = report(sides[0], sides[1])
    print(table)
    if args.json is not None:
        args.json.write_text(json.dumps({"a": sides[0].label, "b": sides[1].label,
                                         "rounds": sides[0].rounds + sides[1].rounds},
                                        indent=1), encoding="utf-8")
    log(f"the copies are in {work}; remove it when done")
    if late and args.fail_slower:
        log(f"slower in B: {', '.join(late)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

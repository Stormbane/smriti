"""H12.1: every write of memory text into the tree goes through one chokepoint.

``smriti.core.memwrite`` masks credential shapes in whatever it writes, so
the guarantee holds only if nothing else writes memory text. The first test
scans the source (``src/smriti`` and the reference profile) for file writes
and fails on any outside the chokepoint and the allowlist below, which
names the writers of runtime and index state, each with its reason.
``tests/test_masked_paths.py`` plants a credential shape in each path into
the tree and finds it masked in the file.

What the scan counts as a write: ``write_text`` and ``write_bytes``; ``open``
(the builtin, ``Path.open``, ``os.fdopen``, ``io.open``...) with a mode that
writes, or a mode it can't read; ``os.open`` and ``os.write``;
``smriti.core.files``' ``write_atomic`` and ``create_new``; ``shutil``'s
copies and moves; and ``tempfile``'s files. A rename moves bytes some other
write put there, and the index is written through SQLite, so neither is a
write here.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED = [REPO / "src", REPO / "profiles" / "reference" / "src"]
CHOKEPOINT = "smriti/core/memwrite.py"

# (module, function) -> why its writes aren't memory text. "*" covers the module.
ALLOWED: dict[tuple[str, str], str] = {
    ("smriti/core/files.py", "*"):
        "the atomic replace and exclusive create the chokepoint writes through",
    ("smriti/core/oslock.py", "*"): "lock files in the private runtime directory",
    ("smriti/embed/runtime.py", "*"):
        "the embedding service's key and discovery file, in the runtime directory",
    ("smriti/embed/client.py", "ServiceEmbedding.start_service"):
        "the service's spawn throttle stamp, in the runtime directory",
    ("smriti/embed/client.py", "spawn_service"): "the service's log, in the runtime directory",
    ("smriti/profiles/__init__.py", "_write_cache"):
        "the profile lookup's cache, in the runtime directory",
    ("smriti/daylog/writer.py", "*"):
        "the day-log JSONL: capture masks each turn before it is appended",
    ("smriti/daylog/state.py", "DaylogState.save"):
        "capture's offsets and dirty days (log/.state.json)",
    ("smriti/daylog/logd.py", "ensure_running"): "logd's own log (log/.logd.log)",
    ("smriti/daylog/logd.py", "run_logd"): "logd's pid file",
    ("smriti/daylog/nightly.py", "_write_status"):
        "the nightly's status document, every string in it masked first "
        "(log/.nightly-status.json)",
    ("smriti/daylog/nightly.py", "_save_marker"):
        "the stale-day scan's marker: each day-file's size, mtime, turns' hash and corrupt-line "
        "count (.smriti/nightly-scan.json)",
    ("smriti/daylog/morning.py", "_write_status"):
        "the morning's status, every string in it masked first (log/.morning-status.json)",
    ("smriti/daylog/morning.py", "_claim_day"): "the morning's ledger entry: a JSON state",
    ("smriti/daylog/morning.py", "_commit"): "the morning's ledger entry: a JSON state",
    ("smriti/daylog/morning.py", "_settle"): "the morning's ledger entry: a JSON state",
    ("smriti/metrics.py", "MetricsLogger.log"):
        "the metrics log: event names, sizes and timings, the write's source masked first",
    ("smriti/witness.py", "write_manifest"): "the witness manifest: a commit and tree hash",
    ("smriti/witness.py", "_save"): "the OpenTimestamps proof of that manifest's hash",
    ("smriti/llm/providers/claude_cli.py", "ClaudeCliProvider.call"):
        "the call's system prompt, in the isolated LLM work directory",
    ("smriti/llm/workdir.py", "*"): "the isolated LLM work directory's settings",
    ("smriti/cli.py", "_cmd_eval"): "a search-evaluation report, at a path the user names",
    ("smriti/eval/runner.py", "*"): "a sandbox copy of the evaluation fixture and index",
    ("smriti/install/core.py", "*"): "the installer: smriti's own templates and scripts",
    ("smriti/install/privacy.py", "*"): "the installer's privacy check: its answer and a listing",
    ("smriti/integrations/common/config_file.py", "*"):
        "a harness's own config (~/.claude.json, settings.json, config.toml) and its "
        "backups, outside the tree",
    ("smriti/integrations/hermes/install.py", "*"): "Hermes' config, outside the tree (legacy)",
    ("smriti/integrations/common/hook_scripts.py", "*"): "the deployed hook scripts",
    ("smriti/integrations/common/managed_doc.py", "*"):
        "the managed block in a project's CLAUDE.md or AGENTS.md, outside the tree",
    ("smriti/integrations/claude_code/hooks/precompact_capture.py", "*"):
        "the legacy PreCompact hook: a staging tree in ~/.claude, outside the memory tree",
}

_WRITE_METHODS = {"write_text", "write_bytes"}
_HELPERS = {"write_atomic", "create_new"}
_SHUTIL = {"copy", "copy2", "copyfile", "copytree", "move", "copyfileobj"}
_TEMPFILE = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp"}
# Modules whose open() takes the path first and the mode second, as the builtin does.
_PATH_THEN_MODE = {"io", "gzip", "bz2", "lzma", "codecs", "tarfile", "os"}


def _mode_writes(call: ast.Call, index: int) -> bool:
    """Whether the open() *call* opens for writing; a mode it can't read counts."""
    mode = next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
    if mode is None and len(call.args) > index:
        mode = call.args[index]
    if mode is None:
        return False  # the default, "r"
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return True


def _write_kind(call: ast.Call) -> str:
    """What kind of file write *call* is, or "" if it is none."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            return "open" if _mode_writes(call, 1) else ""
        return func.id if func.id in _HELPERS | _TEMPFILE else ""
    if not isinstance(func, ast.Attribute):
        return ""
    owner = func.value.id if isinstance(func.value, ast.Name) else ""
    name = func.attr
    if name in _WRITE_METHODS or name in _HELPERS:
        return name
    if owner == "os" and name in ("open", "write"):
        return f"os.{name}"
    if owner == "os" and name == "fdopen":
        return "os.fdopen" if _mode_writes(call, 1) else ""
    if owner == "shutil" and name in _SHUTIL:
        return f"shutil.{name}"
    if owner == "tempfile" and name in _TEMPFILE:
        return f"tempfile.{name}"
    if name == "open":
        index = 1 if owner in _PATH_THEN_MODE else 0  # Path.open(mode)
        return "open" if _mode_writes(call, index) else ""
    return ""


class _Finder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: list[tuple[str, int, str]] = []  # (qualname, line, kind)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        kind = _write_kind(node)
        if kind:
            self.found.append((".".join(self.scope) or "<module>", node.lineno, kind))
        self.generic_visit(node)


def _writes() -> Iterator[tuple[str, str, int, str]]:
    """Every file write in the scanned source: (module, qualname, line, kind)."""
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            module = path.relative_to(base).as_posix()
            finder = _Finder()
            finder.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
            for qualname, line, kind in finder.found:
                yield module, qualname, line, kind


def _allowed(module: str, qualname: str) -> bool:
    if module == CHOKEPOINT or (module, "*") in ALLOWED:
        return True
    # A nested function is covered by the function it is in.
    parts = qualname.split(".")
    return any((module, ".".join(parts[:n])) in ALLOWED for n in range(1, len(parts) + 1))


def test_memory_text_reaches_the_tree_only_through_the_chokepoint() -> None:
    writes = list(_writes())
    stray = [f"{m}:{line} {q}: {kind}" for m, q, line, kind in writes if not _allowed(m, q)]
    assert not stray, ("a file write outside smriti.core.memwrite: route memory text "
                       "through it, or allowlist a writer of state, with its reason\n  "
                       + "\n  ".join(stray))
    assert any(m == CHOKEPOINT for m, *_ in writes)  # the scan sees writes at all


def test_every_allowlist_entry_still_matches_a_write() -> None:
    writes = list(_writes())
    stale = [key for key in ALLOWED
             if not any(m == key[0] and (key[1] == "*" or q == key[1]
                                         or q.startswith(key[1] + "."))
                        for m, q, _line, _kind in writes)]
    assert not stale, stale


def _kind_of(code: str) -> str:
    statement = ast.parse(code).body[0]
    assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    return _write_kind(statement.value)


def test_the_scan_sees_each_kind_of_write() -> None:
    """The scan itself: what it must flag, and what it must leave."""
    flagged = ['Path(p).write_text("x")', 'p.write_bytes(b"x")', 'open(p, "w")',
               'open(p, mode="ab")', 'p.open("x", encoding="utf-8")', 'p.open(mode)',
               'io.open(p, "a")', 'os.open(p, os.O_WRONLY)', 'os.write(fd, b"x")',
               'os.fdopen(fd, "w")', 'write_atomic(p, "x")', 'files.create_new(p, "x")',
               'shutil.copy2(a, b)', 'tempfile.NamedTemporaryFile("w")']
    left = ['open(p)', 'open(p, "rb")', 'p.open()', 'p.open("r", encoding="utf-8")',
            'p.read_text()', 'os.replace(a, b)', 'webbrowser.open_new(u)', 'f.write("x")']
    assert [code for code in flagged if not _kind_of(code)] == []
    assert [code for code in left if _kind_of(code)] == []

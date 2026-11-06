"""H12.0: short processes don't import what they don't use.

Every hook, MCP server and CLI command is a new process, so an import it
doesn't need is paid on every start. These run in fresh interpreters and
check that a module stays unimported by the step that used to pull it in.
A module already imported at startup (a .pth file can do that) passes
trivially: what is checked is that the step itself didn't import it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests._profiles import write_profile_package

SRC = Path(__file__).resolve().parents[1] / "src"


def _imported_by(step: str, modules: list[str], *, env: dict[str, str] | None = None,
                 setup: str = "") -> dict[str, bool]:
    """Which of *modules* running *step* imported, in a fresh interpreter."""
    code = (f"import sys, json\n{setup}\n"
            f"before = {{m: m in sys.modules for m in {modules!r}}}\n"
            f"{step}\n"
            f"print(json.dumps({{m: (m in sys.modules) and not before[m] for m in {modules!r}}}))")
    path = os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": path, **(env or {})}, timeout=120)
    assert done.returncode == 0, done.stderr
    result: dict[str, bool] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_status_loads_neither_sqlite_vec_nor_the_indexer(_isolated_memory_root: Path) -> None:
    """`smriti status` took 650 ms where it had taken 300: sqlite-vec's package
    imports numpy, and the store package imported the indexer and asyncio."""
    from smriti.store.schema import ensure_schema

    ensure_schema(_isolated_memory_root / ".smriti" / "index.db", None).close()
    imported = _imported_by(
        "import contextlib, io\nfrom smriti.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):\n"
        "    main(['status'])",
        # And, with no embedding service running, nothing to reach one with.
        ["numpy", "sqlite_vec", "smriti.store.indexer", "asyncio",
         "socket", "subprocess", "secrets", "hmac", "base64"])
    assert not any(imported.values()), imported


def test_listing_the_tools_does_not_import_the_day_log_writer() -> None:
    """The MCP server reads daylog.json on each request; that mustn't bring the
    day-log writer, its locks and the scrubber."""
    imported = _imported_by(
        "from smriti.mcp.server import handle_message\n"
        "handle_message({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})",
        ["smriti.daylog.writer", "smriti.daylog.scrub", "importlib.metadata"])
    assert not any(imported.values()), imported


def test_a_profile_from_a_warm_cache_does_not_import_importlib_metadata(
        tmp_path: Path) -> None:
    """Importing importlib.metadata alone (it brings the email package) cost
    about 50 ms of every hook and MCP server."""
    site = write_profile_package(tmp_path / "site")
    past = time.time() - 60  # an install long finished: the lookup may be cached
    os.utime(site, (past, past))
    env = {"PYTHONPATH": os.pathsep.join([str(SRC), str(site)]), "PYTHONDONTWRITEBYTECODE": "1"}
    load = ("from smriti.profiles import load_profile\n"
            "assert load_profile('sample').name == 'sample'")

    cold = _imported_by(load, ["importlib.metadata"], env=env)  # scans, writes the cache
    warm = _imported_by(load, ["importlib.metadata"], env=env)

    assert list(Path(os.environ["SMRITI_RUNTIME_DIR"]).glob("profiles-*.json"))
    assert warm == {"importlib.metadata": False}, (cold, warm)

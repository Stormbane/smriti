"""H10.3: long-lived processes embed through the service; degraded states are visible.

The MCP server, ``smriti read`` and hooks ask the memory root's embedding
service for vectors and never load a model; ``index_tree`` and the eval
sandbox embed in-process. When the service is unavailable, a write is still
saved and keyword-searchable at once, a read says it was keyword-only, and
the vectors arrive once the service is back, with no nightly.
"""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.core.tree import tree_root
from smriti.embed.client import ServiceEmbedding, spawn_service, stop_service
from smriti.embed.runtime import (
    Discovery,
    clear_discovery,
    read_discovery,
    runtime_dir,
    service_paths,
    write_discovery,
)
from smriti.store.schema import open_readonly, pending_vectors
from tests.test_embed_service import Services

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture()
def services() -> Iterator[Services]:
    s = Services()
    yield s
    s.stop_all()


@pytest.fixture()
def stub_in_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The in-process model is the stub, whatever the run's provider (and the cache) hold."""
    provider = indexer._InProcess(StubEmbedding())
    monkeypatch.setattr(indexer, "_local_provider", functools.cache(lambda: provider))
    yield


@pytest.fixture()
def tree(stub_in_process: None) -> Path:
    root = tree_root()
    (root / "identity.md").write_text("# Identity\n\nAri keeps the lamps lit.\n",
                                      encoding="utf-8")
    (root / "notes").mkdir()
    (root / "notes" / "garden.md").write_text(
        "# Garden\n\nSam planted marigolds beside the well.\n", encoding="utf-8")
    indexer.index_tree(root=root)
    return root


def _pending() -> int:
    from smriti.core.tree import smriti_db_path

    conn = open_readonly(smriti_db_path())
    try:
        return pending_vectors(conn)
    finally:
        conn.close()


def _call(tool: str, **arguments: Any) -> str:
    from smriti.mcp.server import handle_message

    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": tool, "arguments": arguments}})
    assert reply is not None
    assert not reply["result"].get("isError"), reply
    return str(reply["result"]["content"][0]["text"])


def _has_vec() -> bool:
    from smriti.core.tree import smriti_db_path

    conn = open_readonly(smriti_db_path())
    try:
        return conn.has_vec
    finally:
        conn.close()


# ── Who embeds where ─────────────────────────────────────────────────


def test_long_lived_processes_ask_the_service_and_index_runs_do_not(
        tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    assert isinstance(indexer._get_embedding_provider(), ServiceEmbedding)
    with indexer.in_process_embeddings():
        assert not isinstance(indexer._get_embedding_provider(), ServiceEmbedding)

    def no_service(*a: object, **k: object) -> object:
        raise AssertionError("index_tree asked the service")

    monkeypatch.setattr(ServiceEmbedding, "embed_with_identity", no_service)
    (tree / "notes" / "garden.md").write_text("# Garden\n\nThe marigolds bloomed.\n",
                                              encoding="utf-8")
    assert indexer.index_tree(root=tree)["indexed"] == 1
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "0")
    assert not isinstance(indexer._get_embedding_provider(), ServiceEmbedding)


def test_the_eval_sandbox_embeds_in_process(
        stub_in_process: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.eval.runner import run_search_cases

    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")

    def no_service(*a: object, **k: object) -> object:
        raise AssertionError("the eval asked the service")

    monkeypatch.setattr(ServiceEmbedding, "embed_with_identity", no_service)
    assert run_search_cases()  # no service started for the disposable copy


# ── Outage and recovery ──────────────────────────────────────────────


def test_an_outage_degrades_visibly_and_vectors_fill_in_on_recovery_without_a_nightly(
        tree: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    if not _has_vec():
        pytest.skip("sqlite-vec can't load here")
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    paths = service_paths(runtime_dir(), tree)
    # The service failed to start (its model won't load, say): clients learn it at once.
    write_discovery(paths, Discovery(os.getpid(), "failed", "gone", error="test outage"))

    wrote = _call("smriti_write", content="Ari hung a brass lantern by the door.",
                  branch="notes", title="Lantern")
    assert "saved; indexed for keyword search; vectors pending" in wrote
    assert "test outage" in wrote and _pending() == 1
    read = _call("smriti_read", query="brass lantern")
    assert read.startswith("keyword-only: ") and "test outage" in read
    assert "brass lantern" in read  # searchable at once
    status = _call("smriti_status")
    assert "Vectors pending: 1" in status and "failed to start" in status

    clear_discovery(paths, "gone")
    svc = services.start(tree, pending_poll_s=0.05)  # the service is back
    deadline = time.monotonic() + 10
    while _pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _pending() == 0 and svc.stats.repaired == 1  # no nightly, no index run, no write
    read = _call("smriti_read", query="brass lantern")
    assert not read.startswith("keyword-only") and "brass lantern" in read
    status = _call("smriti_status")
    assert "Vectors pending: 0" in status and "Embedding service: ready" in status
    wrote = _call("smriti_write", content="A second note, with the service up.",
                  branch="notes")
    assert "saved and indexed" in wrote


def test_with_the_service_off_status_says_so(tree: Path) -> None:
    status = _call("smriti_status")
    assert "Embedding service: off" in status and "Vectors pending: 0" in status
    assert "saved and indexed" in _call("smriti_write", content="Plain in-process write.",
                                        branch="notes")


def test_smriti_status_shows_pending_vectors_and_the_service(
        tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from smriti.cli import main

    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Pending:    0 sources" in out and "Embedding:  not running" in out


# ── A real MCP server ────────────────────────────────────────────────

_WRAPPER = """
import atexit, json, os, runpy, sys

def report():
    # (numpy is not on the list: the sqlite_vec package imports it.)
    heavy = sorted(m for m in sys.modules
                   if m.split(".")[0] in ("torch", "onnxruntime", "sentence_transformers",
                                          "transformers", "tokenizers")
                   or m.startswith("smriti._vendored.memsearch.embeddings"))
    rss = None
    try:
        import psutil
        rss = psutil.Process().memory_info().rss
    except ImportError:
        if sys.platform != "win32":
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss = peak if sys.platform == "darwin" else peak * 1024
    with open(os.environ["MCP_REPORT"], "w", encoding="utf-8") as f:
        json.dump({"heavy": heavy, "rss": rss}, f)

atexit.register(report)
sys.argv = ["smriti.mcp_server"]
runpy.run_module("smriti.mcp_server", run_name="__main__")
"""


def test_an_mcp_server_stays_small_and_never_loads_a_model(
        tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """66 servers held about 32 GB when each loaded its own model (H10)."""
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    wrapper = tmp_path / "mcp_wrapper.py"
    wrapper.write_text(_WRAPPER, encoding="utf-8")
    report = tmp_path / "report.json"
    env = dict(os.environ, MCP_REPORT=str(report), SMRITI_EMBED_PROVIDER="stub",
               PYTHONPATH=os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH"))
                                          if p))
    calls = [("smriti_read", {"query": "marigolds well"}),
             ("smriti_write", {"content": "Ari polished the brass lantern.", "branch": "notes"}),
             ("smriti_read", {"query": "brass lantern"}),
             ("smriti_write", {"content": "Sam fed the hens.", "branch": "journal"}),
             ("smriti_read", {"query": "hens"}),
             ("smriti_status", {})]
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    lines += [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
              for i, (name, args) in enumerate(calls, 1)]
    try:
        done = subprocess.run([sys.executable, str(wrapper)], input="\n".join(lines) + "\n",
                              capture_output=True, text=True, env=env, timeout=180,
                              encoding="utf-8")
        replies = {r["id"]: r for r in map(json.loads, done.stdout.splitlines())}
        texts = {i: replies[i]["result"]["content"][0]["text"] for i in range(1, len(calls) + 1)}
        assert all(not replies[i]["result"].get("isError") for i in texts), texts
        measured = json.loads(report.read_text(encoding="utf-8"))
    finally:
        print(stop_service(tree))
        found = read_discovery(service_paths(runtime_dir(), tree))
        if found is not None:
            subprocess.run(["taskkill", "/F", "/PID", str(found.pid)] if sys.platform == "win32"
                           else ["kill", "-9", str(found.pid)], capture_output=True)

    assert measured["heavy"] == [], measured["heavy"]  # no model, no runtime, no provider
    if measured["rss"] is not None:
        assert measured["rss"] < 100 * 2**20, measured["rss"]
    assert "saved and indexed" in texts[2] and "saved and indexed" in texts[4]
    vec = "sqlite-vec is not available" not in texts[5]
    assert not texts[5].startswith("keyword-only") or not vec, texts[5]
    assert "hens" in texts[5]
    assert "Embedding service: ready" in texts[6]


# ── The real model ───────────────────────────────────────────────────


@pytest.mark.needs_model
def test_with_the_real_model_vectors_match_and_a_warm_first_search_is_fast(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("sentence_transformers")
    from smriti.store.search import search_with_status

    root = tree_root()
    for name, text in (("identity.md", "# Identity\n\nAri keeps the lamps lit at night.\n"),
                       ("garden.md", "# Garden\n\nSam planted marigolds beside the well.\n"),
                       ("kitchen.md", "# Kitchen\n\nBread rises slowly in the cold.\n")):
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "local")  # the reference install's model
    indexer._forget_provider()
    service_pid = 0
    try:
        indexer.index_tree(root=root)  # in-process, as the nightly does
        with indexer.in_process_embeddings():
            local = indexer._get_embedding_provider()
            db = open_readonly(root / ".smriti" / "index.db")
            try:
                in_process = search_with_status(db, "flowers near the water", use_reranker=False)
            finally:
                db.close()
        texts = ["flowers near the water", "a light kept burning", "baking in winter"]
        expected = indexer._embed(local, texts, "query")

        paths = service_paths(runtime_dir(), root)
        monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
        monkeypatch.delenv("SMRITI_EMBED_PROVIDER")  # the service follows the index's record
        service_pid = spawn_service(paths).pid
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            found = read_discovery(paths)
            if found is not None and found.state in ("ready", "failed"):
                break
            time.sleep(0.2)
        assert found is not None and found.state == "ready", found
        got = ServiceEmbedding(root, spawn=False).embed_with_identity(texts, kind="query")
        assert got.identity == expected.identity
        for a, b in zip(got.vectors, expected.vectors, strict=True):
            cos = sum(x * y for x, y in zip(a, b, strict=True)) / (
                math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
            assert cos > 0.9999

        # The first search of a new process, with the service warm.
        probe = (
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "from smriti.store.schema import open_readonly\n"
            "from smriti.store.search import search_with_status\n"
            "db = open_readonly(Path(sys.argv[1]))\n"
            "t0 = time.perf_counter()\n"
            "o = search_with_status(db, 'flowers near the water', use_reranker=False)\n"
            "print(json.dumps({'s': time.perf_counter() - t0, 'k': o.keyword_only,\n"
            "                  'top': [r.source for r in o.results],\n"
            "                  'heavy': [m for m in sys.modules if m.split('.')[0] in\n"
            "                            ('torch', 'onnxruntime', 'sentence_transformers')]}))\n")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            p for p in (str(SRC), os.environ.get("PYTHONPATH")) if p))
        out = subprocess.run([sys.executable, "-c", probe, str(root / ".smriti" / "index.db")],
                             capture_output=True, text=True, env=env, timeout=120)
        first = json.loads(out.stdout.strip().splitlines()[-1])
        print(f"first search in a new process, service warm: {first['s'] * 1000:.0f} ms")
        assert first["k"] == "" and first["heavy"] == [], first
        assert first["s"] < 1.0, first
        assert first["top"] == [r.source for r in in_process.results]
    finally:
        print(stop_service(root))
        if service_pid:
            subprocess.run(["taskkill", "/F", "/PID", str(service_pid)]
                           if sys.platform == "win32" else ["kill", "-9", str(service_pid)],
                           capture_output=True)
        indexer._forget_provider()  # later tests load their own model again

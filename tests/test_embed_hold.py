"""H12.0b: the model stays loaded while sessions are open.

Before the shared service, each MCP server held its own model for the
session's life. The service exits after ``idle_s`` without a request, so a
long-lived client holds it: a workless, authenticated ``hold`` every
``idle_s / 3`` from its first embedding on. The service exits ``idle_s``
after the last request or hold; a hold never undoes a drain; a service from
before holds is left to idle out; and however many clients hold, there is
one service.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import smriti.embed
import smriti.embed.client as client_mod
import smriti.store.indexer as indexer
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.core.tree import tree_root
from smriti.embed.client import ServiceEmbedding, service_state, stop_service
from smriti.embed.identity import EmbeddingUnavailable
from smriti.embed.runtime import read_discovery, runtime_dir, service_paths
from smriti.embed.service import EmbedService
from tests.test_embed_service import SRC, Services, _hello, _kill, _service_env, _wait, stub

IDLE = 0.6  # the services' idle exit here: a hold every 0.2 s


@pytest.fixture()
def services() -> Iterator[Services]:
    s = Services()
    yield s
    s.stop_all()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / ".smriti").mkdir()
    return tmp_path


def _gone(services: Services, svc: EmbedService, seconds: float) -> bool:
    return _wait(lambda: svc.instance in services.reasons, seconds)


def _drop_kept(client: ServiceEmbedding) -> None:
    """Close the connection a client keeps for its next request: an open one keeps the
    service by itself, for up to its keep-alive."""
    with client._kept_lock:
        conn, client._kept = client._kept, None
    if conn is not None:
        conn.close()


# ── What a hold does ─────────────────────────────────────────────────


def test_a_client_that_only_holds_keeps_the_service_past_its_idle_exit(
        root: Path, services: Services) -> None:
    # No keep-alive: an open connection would keep the service by itself.
    svc = services.start(root, idle_s=IDLE, keepalive_s=0)
    assert _hello(svc.port)["hold"] is True  # advertised before authentication
    client = ServiceEmbedding(root, spawn=False, hold=True)
    client.embed_with_identity(["Ari lights the first lamp"], kind="query")

    time.sleep(IDLE * 4)  # four idle periods with nothing but holds

    assert svc.instance not in services.reasons  # still running
    assert svc.stats.served == 1 and svc.stats.held >= 4
    client.close()  # the session ends: no more holds
    t0 = time.monotonic()
    assert _gone(services, svc, 5)
    assert "idle" in services.reasons[svc.instance]
    assert time.monotonic() - t0 < IDLE + 1.5  # idle_s after the last hold


def test_with_no_holds_and_no_requests_the_service_exits(
        root: Path, services: Services) -> None:
    svc = services.start(root, idle_s=IDLE, keepalive_s=0)
    one_off = ServiceEmbedding(root, spawn=False, hold=False)  # smriti read, a hook
    one_off.embed_with_identity(["Sam asks once"], kind="query")
    never = ServiceEmbedding(root, spawn=False, hold=True)  # a session that never searches

    assert _gone(services, svc, IDLE + 3)
    assert "idle" in services.reasons[svc.instance]
    assert svc.stats.held == 0 and not one_off.is_holding and not never.is_holding


def test_the_hold_thread_starts_at_the_first_embedding_and_follows_the_process(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = services.start(root)
    # A failed embedding (no service yet for this client) doesn't start it.
    elsewhere = ServiceEmbedding(root / "nowhere", spawn=False, hold=True)
    with pytest.raises(EmbeddingUnavailable):
        elsewhere.embed_with_identity(["lost"])
    assert not elsewhere.is_holding

    # The default follows the process: off unless it said hold_while_running().
    plain = ServiceEmbedding(root, spawn=False)
    plain.embed_with_identity(["the CLI"])
    assert not plain.is_holding and svc.stats.held == 0

    monkeypatch.setattr(smriti.embed, "_holding", False)
    smriti.embed.hold_while_running()  # what the MCP server does at startup
    assert smriti.embed.holding()
    server = ServiceEmbedding(root, spawn=False)
    time.sleep(0.3)
    assert not server.is_holding and svc.stats.held == 0  # nothing before its first embedding
    server.embed_with_identity(["the first search"], kind="query")
    assert server.is_holding
    holder = server._holder
    assert holder is not None and holder.daemon  # never keeps a process from exiting
    assert _wait(lambda: svc.stats.held == 1, 3)  # one at once; the next in idle_s / 3
    server.close()
    assert not server.is_holding and not holder.is_alive()
    plain.close()


def test_a_dropped_client_stops_its_hold_thread(root: Path, services: Services) -> None:
    import gc

    services.start(root)
    client = ServiceEmbedding(root, spawn=False, hold=True)
    client.embed_with_identity(["held"])
    holder = client._holder
    assert holder is not None
    del client
    gc.collect()
    holder.join(5)
    assert not holder.is_alive()  # the thread kept only a weak reference


# ── Drains and restarts ──────────────────────────────────────────────


class Tiny:
    model_name, dimension = "tiny-8", 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 7 for _ in texts]


def test_a_hold_does_not_keep_a_draining_service_and_follows_the_next(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "a.md").write_text("# A\n\nSome text.\n", encoding="utf-8")
    db = root / ".smriti" / "index.db"
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: StubEmbedding())
    indexer.index_tree(root=root, db=db)
    first = services.start(root, idle_s=IDLE, keepalive_s=0, identity_check_s=0.05)
    client = ServiceEmbedding(root, hold=True)
    started: list[EmbedService] = []

    def start(paths: Any, **kw: Any) -> bool:
        started.append(services.start(root, Tiny(), idle_s=IDLE, keepalive_s=0))
        return True

    client.start_service = start  # type: ignore[method-assign]
    client.embed_with_identity(["before the migration"])
    assert _wait(lambda: first.stats.held >= 2, 3)

    # The index moves to another model: the service drains, holds or not.
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: Tiny())
    indexer.index_tree(full=True, root=root, db=db)
    assert _gone(services, first, 5)
    assert "index now records" in services.reasons[first.instance]

    # The hold thread finds it gone and starts the next, which it then holds.
    assert _wait(lambda: len(started) == 1, 3)
    second = started[0]
    time.sleep(IDLE * 3)
    assert len(started) == 1 and second.instance not in services.reasons
    assert second.stats.held >= 3 and second.stats.served == 0  # warm, and nothing asked yet
    assert client.embed_with_identity(["after"]).identity.model == "tiny-8"
    client.close()

    # A hold that reaches a service already draining changes nothing.
    second.stop("smriti embed stop")
    assert _gone(services, second, 5)
    before = second._last_request
    reply = second._handle({"op": "hold"})
    assert reply["ok"] is True and reply["state"] == "draining"
    assert second._last_request == before


def test_a_hold_to_an_older_service_is_stopped_until_the_instance_changes(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(client_mod, "HOLD_EVERY_S", 0.1)  # until a service says its idle_s
    old = services.start(root, keepalive_s=0, hold=False)  # a service from before holds
    assert "hold" not in _hello(old.port)
    ops: list[object] = []
    real_handle = old._handle
    monkeypatch.setattr(old, "_handle", lambda request: ops.append(request.get("op"))
                        or real_handle(request))
    client = ServiceEmbedding(root, spawn=False, hold=True)
    caplog.set_level(logging.DEBUG, logger="smriti.embed.client")

    client.embed_with_identity(["to an old service"])
    time.sleep(1.0)  # about ten hold intervals

    assert ops.count("hold") == 1  # asked once: "unknown op"; never again
    assert ops.count("embed") == 1
    assert client.is_holding  # still watching for the next instance
    said = [r for r in caplog.records if "can't be held" in r.getMessage()]
    assert len(said) == 1 and said[0].levelno == logging.DEBUG

    old.stop("upgraded")
    assert _gone(services, old, 5)
    new = services.start(root, idle_s=IDLE, keepalive_s=0)
    assert _wait(lambda: new.stats.held >= 2, 3)  # the new instance is held
    client.close()


# ── Processes ────────────────────────────────────────────────────────


def _readline(stream: Any, seconds: float) -> str:
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(stream.readline()), daemon=True)
    t.start()
    t.join(seconds)
    assert out, "no reply in time"
    return out[0]


def test_an_mcp_server_holds_from_its_first_embedding_and_still_exits_at_once(
        services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tree_root()
    (root / ".smriti").mkdir(parents=True, exist_ok=True)
    svc = services.start(root)  # in this process; the server finds it through discovery
    monkeypatch.setenv("SMRITI_EMBED_SERVICE", "1")
    env = dict(os.environ, SMRITI_EMBED_PROVIDER="stub",
               PYTHONPATH=os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH"))
                                          if p))
    proc = subprocess.Popen([sys.executable, "-m", "smriti.mcp_server"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                     "params": {}}) + "\n")
        proc.stdin.flush()
        assert json.loads(_readline(proc.stdout, 60))["id"] == 0
        time.sleep(0.5)
        assert svc.stats.held == 0  # started, but nothing embedded yet

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "smriti_write", "arguments": {
                                         "content": "Ari mended the gate.",
                                         "branch": "notes"}}}) + "\n")
        proc.stdin.flush()
        reply = json.loads(_readline(proc.stdout, 60))
        assert "saved" in reply["result"]["content"][0]["text"], reply
        assert _wait(lambda: svc.stats.held >= 1, 5)  # its hold thread runs

        proc.stdin.close()  # the session ends
        t0 = time.monotonic()
        assert proc.wait(15) == 0
        assert time.monotonic() - t0 < 5  # not held up by a thread waiting out its interval
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_one_service_however_many_clients_hold_it(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = runtime_dir()
    for key, value in _service_env(runtime).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SMRITI_EMBED_IDLE_S", "1.5")  # the spawned services' idle exit
    paths = service_paths(runtime, root)
    clients = [ServiceEmbedding(root, hold=True) for _ in range(3)]

    def alive_services() -> int:
        return sum(p.poll() is None for c in clients for p in c._children)

    try:
        for i, client in enumerate(clients):  # the first starts it; the others find it
            assert client.embed_with_identity([f"session {i}"]).vectors == stub([f"session {i}"])
            _drop_kept(client)  # from here on, only the holds keep it
        found = read_discovery(paths)
        assert found is not None and found.state == "ready"

        time.sleep(5)  # more than three idle periods, with only holds

        now = read_discovery(paths)
        assert now is not None and now.instance == found.instance and now.state == "ready"
        state = service_state(root)
        assert state.live is not None and state.live["stats"]["held"] >= 9
        assert alive_services() == 1

        # It goes (`smriti embed stop`, a crash): the holders start one, not three.
        print(stop_service(root))
        assert _wait(lambda: (d := read_discovery(paths)) is not None and d.state == "ready"
                     and d.instance != found.instance, 60)
        time.sleep(3)  # every holder has looked again
        assert alive_services() == 1
        replaced = read_discovery(paths)
        assert replaced is not None and replaced.state == "ready"

        # The last session ends: the service exits idle_s later.
        for client in clients:
            client.close()
        closed = time.monotonic()
        assert _wait(lambda: read_discovery(paths) is None, 20), (
            time.monotonic() - closed, read_discovery(paths), service_state(root).describe(),
            alive_services(), [p.pid for c in clients for p in c._children if p.poll() is None])
        assert _wait(lambda: alive_services() == 0, 15)
    finally:
        for client in clients:
            client.close()
        print(stop_service(root))
        _kill([p for c in clients for p in c._children])


class _Waits:
    """A stop event that records each wait and stops after the last expected one."""

    def __init__(self, stop_after: int) -> None:
        self.waits: list[float] = []
        self.stop_after = stop_after

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return len(self.waits) > self.stop_after


class _Outcomes:
    """Stands in for a client: each hold_once gives the next outcome; "held" teaches 20 s."""

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self._hold_every = client_mod.HOLD_EVERY_S

    def hold_once(self) -> str:
        outcome = self.outcomes.pop(0)
        if outcome == "held":
            self._hold_every = 20.0
        return outcome


def test_a_replacement_service_is_held_before_its_shorter_idle_runs_out() -> None:
    # An older service refuses holds; when it goes, the loop starts a new one
    # whose idle_s may be shorter than the last interval. Holding it again only
    # after that interval let it idle out unheld, over and over (Codex review).
    fake = _Outcomes(["refused: unknown op 'hold'", "gone; started another", "starting",
                      "took no connection: refused", "held", "held"])
    stop = _Waits(stop_after=6)
    client_mod._hold_loop(lambda: fake, stop)  # type: ignore[arg-type, return-value]
    every = client_mod.HOLD_EVERY_S
    assert stop.waits == [0.0, every, 2.0, 4.0, 8.0, 20.0, 20.0]

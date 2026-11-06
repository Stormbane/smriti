"""H10.2: the embedding service, its transport and its lifecycle.

Services run in threads of this process (or, where processes matter, as
subprocesses) against temporary roots and runtime directories, with the
stub provider: no test here loads a real model.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed.client import ServiceEmbedding, service_state, stop_service
from smriti.embed.identity import EmbeddingIdentity, EmbeddingUnavailable
from smriti.embed.protocol import (
    MAX_FRAME,
    PREAUTH_MAX,
    PROTOCOL,
    client_proof,
    recv_frame,
    send_frame,
)
from smriti.embed.runtime import (
    Discovery,
    RuntimeDirError,
    clear_discovery,
    load_key,
    private_dir_problem,
    read_discovery,
    runtime_dir,
    service_paths,
    write_discovery,
)
from smriti.embed.service import EmbedService

STUB = EmbeddingIdentity("stub", "stub-384", "", 384)
SRC = Path(__file__).resolve().parents[1] / "src"


def stub(texts: list[str]) -> list[list[float]]:
    return asyncio.run(StubEmbedding().embed(texts))


class Services:
    """In-thread services, holding the root's lock and published as ``serve`` would."""

    def __init__(self) -> None:
        self.running: list[tuple[EmbedService, threading.Thread]] = []
        self.reasons: dict[str, str] = {}

    def start(self, root: Path, provider: Any = None, *, publish: bool = True,
              **kw: Any) -> EmbedService:
        from smriti.core.oslock import exclusive_lock
        runtime = runtime_dir()
        paths = service_paths(runtime, root)
        holder: list[EmbedService] = []

        def on_state(state: str) -> None:
            svc = holder[0]
            write_discovery(paths, Discovery(os.getpid(), state, svc.instance, port=svc.port,
                                             identity=svc.identity, root=str(paths.root)))

        svc = EmbedService(provider=provider or StubEmbedding(), key=load_key(runtime),
                           db=root / ".smriti" / "index.db",
                           on_state=on_state if publish else None, **kw)
        holder.append(svc)
        svc.bind()

        def run() -> None:
            # Like serve(): the lock for the service's life (a draining
            # predecessor may still hold it for a moment).
            with exclusive_lock(paths.lock, wait_s=15) as held:
                assert held, "another service holds the lock"
                reason = svc.run()
                if publish:
                    clear_discovery(paths, svc.instance)
            self.reasons[svc.instance] = reason  # last: once it is here, the service is gone

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while svc.state != "ready" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.running.append((svc, t))
        return svc

    def stop_all(self) -> None:
        for svc, _ in self.running:
            svc.stop("the test is over")
        for _, t in self.running:
            t.join(15)


@pytest.fixture()
def services() -> Iterator[Services]:
    s = Services()
    yield s
    s.stop_all()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / ".smriti").mkdir()
    return tmp_path


def _raw_authenticated(port: int, key: bytes) -> socket.socket:
    """A peer that knows the key and completes the handshake by hand."""
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    nonce = secrets.token_hex(16)
    send_frame(s, {"op": "hello", "proto": PROTOCOL, "nonce": nonce}, time.monotonic() + 2)
    hello = recv_frame(s, time.monotonic() + 2, PREAUTH_MAX)
    send_frame(s, {"op": "auth", "proof": client_proof(key, nonce, hello["nonce"])},
               time.monotonic() + 2)
    return s


def _closed_within(s: socket.socket, seconds: float) -> bool:
    """True when the server closes ``s`` within ``seconds``."""
    s.settimeout(seconds)
    try:
        return s.recv(1) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True
    except TimeoutError:
        return False


def _wait(cond: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ── Round trip ───────────────────────────────────────────────────────


def test_round_trip_gives_the_vectors_an_in_process_model_would(
        root: Path, services: Services) -> None:
    svc = services.start(root)
    client = ServiceEmbedding(root, spawn=False)
    texts = ["Ari keeps the lamps lit.", "Sam planted marigolds beside the well.", ""]

    got = client.embed_with_identity(texts, kind="query")

    assert got.identity == STUB
    assert got.vectors == stub(texts)  # identical, not merely close
    assert client.last_instance == svc.instance and svc.stats.served == 1
    many = [f"text number {i}" for i in range(150)]  # more than one request's worth
    assert client.embed_with_identity(many).vectors == stub(many)
    assert svc.stats.served == 1 + 3


def test_a_wrong_key_is_refused_both_ways(root: Path, services: Services,
                                          tmp_path_factory: pytest.TempPathFactory,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    svc = services.start(root)
    paths = service_paths(runtime_dir(), root)
    real_key = load_key(runtime_dir())

    # A client holding another key finds the service's proof wrong, and sends nothing.
    other = tmp_path_factory.mktemp("other-run")
    other_paths = service_paths(other, root)
    other_paths.discovery.write_text(paths.discovery.read_text(encoding="utf-8"),
                                     encoding="utf-8")
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(other))
    assert load_key(other) != real_key
    with pytest.raises(EmbeddingUnavailable, match="failed authentication"):
        ServiceEmbedding(root, spawn=False).embed_with_identity(["secret words"])
    assert svc.stats.served == 0 and svc.stats.auth_failures == 0

    # A peer without the key that sends a proof anyway is closed on, unanswered.
    s = socket.create_connection(("127.0.0.1", svc.port), timeout=2)
    with s:
        nonce = secrets.token_hex(16)
        send_frame(s, {"op": "hello", "proto": PROTOCOL, "nonce": nonce}, time.monotonic() + 2)
        hello = recv_frame(s, time.monotonic() + 2, PREAUTH_MAX)
        send_frame(s, {"op": "auth", "proof": client_proof(b"\0" * 32, nonce, hello["nonce"])},
                   time.monotonic() + 2)
        send_frame(s, {"op": "embed", "kind": "query", "texts": ["x"], "budget_ms": 1000},
                   time.monotonic() + 2)
        assert _closed_within(s, 2)
    assert svc.stats.auth_failures == 1 and svc.stats.served == 0


def test_an_impostor_on_the_port_is_detected(root: Path) -> None:
    received: list[dict[str, Any]] = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def impostor() -> None:
        conn, _ = listener.accept()
        with conn:
            try:
                received.append(recv_frame(conn, time.monotonic() + 2, PREAUTH_MAX))
                send_frame(conn, {"proto": PROTOCOL, "instance": "imposter",
                                  "nonce": secrets.token_hex(16), "proof": "0" * 64},
                           time.monotonic() + 2)
                while True:
                    received.append(recv_frame(conn, time.monotonic() + 2, MAX_FRAME))
            except (OSError, ValueError):
                pass

    t = threading.Thread(target=impostor, daemon=True)
    t.start()
    write_discovery(service_paths(runtime_dir(), root),
                    Discovery(os.getpid(), "ready", "real-instance", port=port, identity=STUB))
    try:
        with pytest.raises(EmbeddingUnavailable, match="failed authentication"):
            ServiceEmbedding(root, spawn=False).embed_with_identity(["the private words"])
    finally:
        t.join(5)
        listener.close()
    assert [f.get("op") for f in received] == ["hello"]  # no proof, no texts reached it
    assert "private words" not in repr(received)


# ── Bounds and deadlines ─────────────────────────────────────────────


def test_stalled_handshakes_and_partial_frames_time_out_without_blocking_others(
        root: Path, services: Services) -> None:
    svc = services.start(root, handshake_s=0.5, request_read_s=0.5)
    key = load_key(runtime_dir())
    silent = socket.create_connection(("127.0.0.1", svc.port))
    half_header = socket.create_connection(("127.0.0.1", svc.port))
    half_header.sendall(b"\x00\x00")
    half_frame = socket.create_connection(("127.0.0.1", svc.port))
    half_frame.sendall(struct.pack(">I", 100) + b"0123456789")
    half_request = _raw_authenticated(svc.port, key)
    half_request.sendall(struct.pack(">I", 1000) + b"{\"op\": \"embed\"")
    stalled = [silent, half_header, half_frame, half_request]
    try:
        assert _wait(lambda: svc.active_connections == 4, 2)
        t0 = time.monotonic()
        got = ServiceEmbedding(root, spawn=False).embed_with_identity(["meanwhile"], kind="query")
        assert got.vectors == stub(["meanwhile"]) and time.monotonic() - t0 < 0.4
        for s in stalled:
            assert _closed_within(s, 2.0)
        assert _wait(lambda: svc.active_connections == 0, 2)
    finally:
        for s in stalled:
            s.close()


def test_oversize_frames_and_requests_are_refused(root: Path, services: Services) -> None:
    svc = services.start(root)
    key = load_key(runtime_dir())
    with _raw_authenticated(svc.port, key) as s:  # a frame over the limit: closed, not buffered
        s.sendall(struct.pack(">I", MAX_FRAME + 1))
        assert _closed_within(s, 2)
    with socket.create_connection(("127.0.0.1", svc.port)) as s:  # pre-auth frames are tiny
        s.sendall(struct.pack(">I", PREAUTH_MAX + 1))
        assert _closed_within(s, 2)
    for texts in ([f"t{i}" for i in range(65)], [1, 2], []):
        with _raw_authenticated(svc.port, key) as s:
            send_frame(s, {"op": "embed", "kind": "write", "texts": texts, "budget_ms": 1000},
                       time.monotonic() + 2)
            reply = recv_frame(s, time.monotonic() + 2)
        assert reply["ok"] is False and reply["code"] == "request"
    assert svc.stats.served == 0


class Gated(StubEmbedding):
    """The stub, holding its first call until released, and recording what it embedded."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.seen: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(10)
        self.seen.append(texts)
        return await super().embed(texts)


def test_a_full_queue_says_busy_and_expired_requests_are_dropped_unrun(
        root: Path, services: Services) -> None:
    gated = Gated()
    svc = services.start(root, gated, queue_max=3)
    out: dict[str, Any] = {}

    def call(name: str, text: str, write_s: float) -> None:
        try:
            out[name] = ServiceEmbedding(root, spawn=False, write_s=write_s).embed_with_identity(
                [text])
        except EmbeddingUnavailable as exc:
            out[name] = exc

    first = threading.Thread(target=call, args=("first", "holds the model", 10))
    first.start()
    assert gated.entered.wait(5)
    call("gives-up", "abandoned while queued", 0.3)  # waits behind it, then gives up
    queued = [threading.Thread(target=call, args=(f"q{i}", f"queued {i}", 10)) for i in range(2)]
    for t in queued:
        t.start()
    # The abandoned request still holds its place until the model reaches it.
    assert _wait(lambda: svc._queue.qsize() == 3, 2)
    call("refused", "one too many", 10)
    # Released only once the service itself has given up on the abandoned one
    # (its deadline can trail the client's by the time a frame takes to arrive).
    assert _wait(lambda: svc.stats.timed_out == 1, 5)
    gated.release.set()
    first.join(10)
    for t in queued:
        t.join(10)

    assert isinstance(out["gives-up"], EmbeddingUnavailable)
    assert isinstance(out["refused"], EmbeddingUnavailable) and "busy" in str(out["refused"])
    assert out["first"].vectors == stub(["holds the model"])
    assert all(out[f"q{i}"].vectors == stub([f"queued {i}"]) for i in range(2))
    assert ["abandoned while queued"] not in gated.seen  # dropped, never run
    assert svc.stats.dropped == 1 and svc.stats.busy == 1


def test_a_requests_budget_is_what_is_left_when_it_is_sent(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    """The client sent the budget it had before connecting, so the service's
    deadline outlived the client's by the connect and handshake, and it ran
    work the client had already given up on (seen as a flaky test under load)."""
    import smriti.embed.client as client_mod

    gated = Gated()
    services.start(root, gated)
    first = threading.Thread(target=lambda: ServiceEmbedding(root, spawn=False)
                             .embed_with_identity(["holds the model"]))
    first.start()
    assert gated.entered.wait(5)
    real_send = client_mod.send_frame

    def slow_handshake(sock: socket.socket, frame: dict[str, Any], deadline: float) -> None:
        if frame.get("op") == "auth":
            time.sleep(0.4)  # a handshake slowed by load
        real_send(sock, frame, deadline)

    monkeypatch.setattr(client_mod, "send_frame", slow_handshake)
    t0 = time.monotonic()
    with pytest.raises(EmbeddingUnavailable):
        ServiceEmbedding(root, spawn=False, write_s=0.6).embed_with_identity(["too late"])
    monkeypatch.setattr(client_mod, "send_frame", real_send)
    # After the client's deadline (0.6 s), before the one an early budget gave (1.0 s).
    time.sleep(max(0.0, 0.8 - (time.monotonic() - t0)))
    gated.release.set()
    first.join(10)
    assert _wait(lambda: len(gated.seen) >= 1, 5)
    time.sleep(0.1)
    assert ["too late"] not in gated.seen


def test_queries_go_ahead_of_write_batches(root: Path, services: Services) -> None:
    gated = Gated()
    services.start(root, gated)
    done: list[str] = []

    def call(text: str, kind: str) -> None:
        ServiceEmbedding(root, spawn=False).embed_with_identity([text], kind=kind)
        done.append(text)

    blocker = threading.Thread(target=call, args=("first", "write"))
    blocker.start()
    assert gated.entered.wait(5)
    writes = [threading.Thread(target=call, args=(f"write {i}", "write")) for i in range(3)]
    for t in writes:
        t.start()
    time.sleep(0.2)
    query = threading.Thread(target=call, args=("the query", "query"))
    query.start()
    time.sleep(0.2)
    gated.release.set()
    for t in [blocker, *writes, query]:
        t.join(10)
    assert [t for t in gated.seen if t != ["first"]][0] == ["the query"]


def test_sustained_unauthenticated_connections_stay_bounded_and_clients_degrade_in_time(
        root: Path, services: Services) -> None:
    svc = services.start(root)
    stop = threading.Event()
    peak = [0]

    def flood() -> None:
        held: list[socket.socket] = []
        while not stop.is_set():
            try:
                held.append(socket.create_connection(("127.0.0.1", svc.port), timeout=0.5))
            except OSError:
                time.sleep(0.01)
            if len(held) > 40:
                held.pop(0).close()
        for s in held:
            s.close()

    def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], svc.active_connections,
                          sum(t.name == "smriti-embed-conn" for t in threading.enumerate()))
            time.sleep(0.002)

    threads = [threading.Thread(target=flood) for _ in range(4)] + [threading.Thread(target=watch)]
    for t in threads:
        t.start()
    try:
        time.sleep(1.0)
        outcomes = []
        for _ in range(3):
            t0 = time.monotonic()
            try:
                ServiceEmbedding(root, spawn=False).embed_with_identity(["legit"], kind="query")
                outcomes.append(("served", time.monotonic() - t0))
            except EmbeddingUnavailable as exc:
                outcomes.append((str(exc), time.monotonic() - t0))
    finally:
        stop.set()
        for t in threads:
            t.join(10)
    assert all(elapsed < 3.5 for _, elapsed in outcomes), outcomes  # the 3 s query deadline
    assert peak[0] <= 32 and svc.stats.refused > 0
    assert _wait(lambda: svc.active_connections == 0, 3)
    got = ServiceEmbedding(root, spawn=False).embed_with_identity(["after"], kind="query")
    assert got.vectors == stub(["after"])


# ── Keep-alive (H12.0) ───────────────────────────────────────────────
#
# A connection, and its handshake, used to be paid for every request (the
# connect alone 14-21 ms on Windows). Now one is kept while the service says
# it may be. Every pairing of old and new client and service must still work.


def _hello(port: int) -> dict[str, Any]:
    """The service's answer to a hello: what it advertises before authentication."""
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        send_frame(s, {"op": "hello", "proto": PROTOCOL, "nonce": secrets.token_hex(16)},
                   time.monotonic() + 2)
        return recv_frame(s, time.monotonic() + 2, PREAUTH_MAX)


def _embed_on(s: socket.socket, text: str) -> dict[str, Any]:
    send_frame(s, {"op": "embed", "kind": "query", "texts": [text], "budget_ms": 2000},
               time.monotonic() + 2)
    return recv_frame(s, time.monotonic() + 2)


class Exchanges:
    """Records, for each request a client sends, whether it went on a kept connection."""

    def __init__(self, client: ServiceEmbedding, monkeypatch: pytest.MonkeyPatch) -> None:
        self.kept: list[bool] = []
        real = client._exchange

        def recording(conn: Any, request: Any, deadline: float, *, kept: bool) -> Any:
            self.kept.append(kept)
            return real(conn, request, deadline, kept=kept)

        monkeypatch.setattr(client, "_exchange", recording)


def test_one_connection_carries_a_clients_requests(root: Path, services: Services) -> None:
    svc = services.start(root)
    client = ServiceEmbedding(root, spawn=False)
    texts = [f"Ari counts lantern {i}" for i in range(5)]

    got = [client.embed_with_identity([t], kind="query").vectors for t in texts]

    assert got == [stub([t]) for t in texts]
    assert svc.stats.served == 5 and svc.stats.authenticated == 1
    assert svc.active_connections == 1  # kept for the next request
    client.close()
    assert _wait(lambda: svc.active_connections == 0, 2)


def test_an_old_client_still_gets_one_request_and_frees_its_slot(
        root: Path, services: Services) -> None:
    """A client from before keep-alive sends one request and closes."""
    svc = services.start(root)
    assert _hello(svc.port)["keepalive_s"] == 10.0  # advertised in the hello
    with _raw_authenticated(svc.port, load_key(runtime_dir())) as s:  # as an old client does
        reply = _embed_on(s, "Sam waters the beans")
    assert reply["ok"] is True and svc.stats.served == 1
    t0 = time.monotonic()
    assert _wait(lambda: svc.active_connections == 0, 2)
    assert time.monotonic() - t0 < 1.0  # at its close, not after the keep-alive


def test_a_new_client_against_an_old_service_connects_for_each_request(
        root: Path, services: Services) -> None:
    """A service from before keep-alive advertises none, and closes after one reply."""
    svc = services.start(root, keepalive_s=0)
    assert "keepalive_s" not in _hello(svc.port)
    client = ServiceEmbedding(root, spawn=False)
    for i in range(3):
        assert client.embed_with_identity([f"one {i}"]).vectors == stub([f"one {i}"])
        assert client._kept is None  # nothing kept: an old service would close it
    assert svc.stats.served == 3 and svc.stats.authenticated == 3
    assert _wait(lambda: svc.active_connections == 0, 2)


def test_the_service_closes_a_connection_idle_past_its_keepalive(
        root: Path, services: Services) -> None:
    svc = services.start(root, keepalive_s=0.3)
    with _raw_authenticated(svc.port, load_key(runtime_dir())) as s:
        assert _embed_on(s, "first")["ok"] is True
        t0 = time.monotonic()
        assert _embed_on(s, "within the keep-alive")["ok"] is True  # the same connection
        assert _closed_within(s, 3)
        assert 0.25 <= time.monotonic() - t0 < 2.0
    assert _wait(lambda: svc.active_connections == 0, 2)
    assert svc.stats.authenticated == 1 and svc.stats.served == 2


def test_a_client_stops_reusing_a_connection_before_the_service_closes_it(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = services.start(root, keepalive_s=1.5)  # reusable for 1.5 - 1.0 s of idleness
    client = ServiceEmbedding(root, spawn=False)
    exchanges = Exchanges(client, monkeypatch)

    client.embed_with_identity(["one"])
    client.embed_with_identity(["two"])  # at once: on the kept connection
    time.sleep(0.7)  # idle past the margin, though the service still keeps it
    assert client.embed_with_identity(["three"]).vectors == stub(["three"])

    assert exchanges.kept == [False, True, False]  # never sent on the one near its close
    assert svc.stats.authenticated == 2 and svc.stats.served == 3
    client.close()


def test_a_request_on_a_connection_the_service_closed_goes_once_more_fresh(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.embed.client as client_mod

    monkeypatch.setattr(client_mod, "KEEPALIVE_MARGIN_S", 0.05)
    svc = services.start(root, keepalive_s=0.3)
    client = ServiceEmbedding(root, spawn=False)
    client.embed_with_identity(["before"])
    assert client._kept is not None
    client._kept.keepalive_s = 30.0  # the client believes it fresh...
    assert _wait(lambda: svc.active_connections == 0, 3)  # ...but the service closed it
    # The close lands after the client's look at the connection, as in a race.
    monkeypatch.setattr(client_mod, "_peer_closed", lambda sock: False)
    exchanges = Exchanges(client, monkeypatch)

    got = client.embed_with_identity(["after the close"], kind="query")

    assert got.vectors == stub(["after the close"])
    assert exchanges.kept == [True, False]  # the closed one, then one fresh retry
    assert svc.stats.served == 2  # it ran once
    assert svc.stats.authenticated == 2
    client.close()


def test_a_kept_connection_seen_closed_is_not_sent_on(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    import smriti.embed.client as client_mod

    monkeypatch.setattr(client_mod, "KEEPALIVE_MARGIN_S", 0.05)
    svc = services.start(root, keepalive_s=0.3)
    client = ServiceEmbedding(root, spawn=False)
    client.embed_with_identity(["before"])
    assert client._kept is not None
    client._kept.keepalive_s = 30.0
    assert _wait(lambda: svc.active_connections == 0, 3)
    exchanges = Exchanges(client, monkeypatch)

    assert client.embed_with_identity(["after"]).vectors == stub(["after"])
    assert exchanges.kept == [False]  # its end was seen first: straight to a fresh one
    client.close()


def test_a_restarted_service_gets_a_fresh_connection(root: Path, services: Services) -> None:
    first = services.start(root)
    client = ServiceEmbedding(root, spawn=False)
    client.embed_with_identity(["to the first"])
    assert client._kept is not None and client.last_instance == first.instance
    first.stop("restarted")
    assert _wait(lambda: first.instance in services.reasons, 5)
    second = services.start(root)

    assert client.embed_with_identity(["to the second"]).vectors == stub(["to the second"])
    assert client.last_instance == second.instance and second.stats.authenticated == 1
    client.close()


def test_a_drain_does_not_wait_out_kept_connections(root: Path, services: Services) -> None:
    svc = services.start(root)  # the full 10 s keep-alive
    client = ServiceEmbedding(root, spawn=False)
    client.embed_with_identity(["kept"])
    assert svc.active_connections == 1
    t0 = time.monotonic()
    svc.stop("stopped while a connection is kept")
    assert _wait(lambda: svc.instance in services.reasons, 5)
    assert time.monotonic() - t0 < 2.0
    client.close()


def test_concurrent_calls_on_one_client_never_share_a_connection(
        root: Path, services: Services) -> None:
    svc = services.start(root)
    client = ServiceEmbedding(root, spawn=False)
    start = threading.Barrier(8)
    wrong: list[str] = []

    def calls(n: int) -> None:
        start.wait()
        for i in range(6):
            text = f"Ari's thread {n}, call {i}"
            try:
                if client.embed_with_identity([text], kind="query").vectors != stub([text]):
                    wrong.append(text)
            except EmbeddingUnavailable as exc:
                wrong.append(f"{text}: {exc}")

    threads = [threading.Thread(target=calls, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert wrong == []  # every reply was its own request's
    assert svc.stats.served == 48 and svc.stats.authenticated <= 48
    assert client._kept is not None  # one kept; the others closed
    assert _wait(lambda: svc.active_connections == 1, 3)
    client.close()


class _Swallower:
    """A service that authenticates properly, keeps connections, and answers nothing."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.requests: list[str] = []
        self.connections = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = int(self.listener.getsockname()[1])
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        from smriti.embed.protocol import server_proof

        while not self.done.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self.connections += 1
                try:
                    hello = recv_frame(conn, time.monotonic() + 2, PREAUTH_MAX)
                    nonce = secrets.token_hex(16)
                    send_frame(conn, {"proto": PROTOCOL, "instance": "swallower", "nonce": nonce,
                                      "proof": server_proof(self.key, hello["nonce"], nonce,
                                                            "swallower"),
                                      "keepalive_s": 10.0}, time.monotonic() + 2)
                    recv_frame(conn, time.monotonic() + 2, PREAUTH_MAX)  # the client's proof
                    self.requests.append(str(recv_frame(conn, time.monotonic() + 2).get("op")))
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.done.set()
        self.thread.join(5)
        self.listener.close()


def test_a_stop_goes_on_its_own_connection_and_is_never_sent_twice(
        root: Path, services: Services) -> None:
    from smriti.embed.client import _Gone

    paths = service_paths(runtime_dir(), root)
    key = load_key(runtime_dir())
    svc = services.start(root)
    client = ServiceEmbedding(root, spawn=False)
    client.embed_with_identity(["kept"])
    found = read_discovery(paths)
    assert found is not None and client._kept is not None

    client._call(found, key, paths, time.monotonic() + 5, {"op": "stop"})
    assert svc.stats.authenticated == 2  # not on the kept connection
    assert _wait(lambda: svc.instance in services.reasons, 5)
    client.close()

    # A stop whose connection closes before its answer is not sent again,
    # where an embed (idempotent) is.
    swallower = _Swallower(key)
    try:
        gone = Discovery(os.getpid(), "ready", "swallower", port=swallower.port, identity=STUB)
        with pytest.raises(_Gone):
            client._call(gone, key, paths, time.monotonic() + 5, {"op": "stop"})
        assert swallower.requests == ["stop"] and swallower.connections == 1
        write_discovery(paths, gone)
        with pytest.raises(EmbeddingUnavailable, match="closed the connection twice"):
            client.embed_with_identity(["swallowed"])
        assert swallower.requests == ["stop", "embed", "embed"]
    finally:
        swallower.close()
        client.close()


# ── Lifecycle ────────────────────────────────────────────────────────


def test_idle_exit_racing_a_request_retries_on_a_fresh_service(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    deciding, proceed = threading.Event(), threading.Event()

    def hold_the_drain(reason: str) -> None:
        deciding.set()
        proceed.wait(5)

    paths = service_paths(runtime_dir(), root)
    first = services.start(root, idle_s=0.3, on_drain=hold_the_drain)
    as_read = read_discovery(paths)  # what a client saw just before the service went idle
    assert as_read is not None and as_read.state == "ready"
    client = ServiceEmbedding(root, spawn=True)
    started: list[EmbedService] = []
    client.start_service = lambda paths, **kw: bool(started.append(services.start(root)) or True)
    real_ready = client._ready_service
    looks: list[str] = []

    def ready(paths: Any, deadline: float, kind: str = "write") -> Discovery:
        if not looks:
            looks.append("stale")
            return as_read  # the request goes to the service that is exiting
        looks.append("fresh")
        return real_ready(paths, deadline, kind)

    monkeypatch.setattr(client, "_ready_service", ready)
    assert deciding.wait(5)  # it has decided it is idle; it accepts nothing more
    out: dict[str, Any] = {}

    def request() -> None:
        try:
            out["got"] = client.embed_with_identity(["arrives as it exits"])
        except EmbeddingUnavailable as exc:
            out["got"] = exc

    t = threading.Thread(target=request)
    t.start()
    time.sleep(0.2)  # the request reaches the closing service
    proceed.set()
    t.join(15)

    assert not isinstance(out["got"], Exception), out
    assert out["got"].vectors == stub(["arrives as it exits"])
    assert _wait(lambda: first.instance in services.reasons, 5)
    assert "idle" in services.reasons[first.instance]
    assert looks == ["stale", "fresh"]  # one EOF, one fresh try
    assert len(started) == 1 and client.last_instance == started[0].instance


def test_a_stop_finishes_the_work_in_flight(root: Path, services: Services) -> None:
    gated = Gated()
    svc = services.start(root, gated)
    out: dict[str, Any] = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "got", ServiceEmbedding(root, spawn=False).embed_with_identity(["in flight"])))
    t.start()
    assert gated.entered.wait(5)
    svc.stop("smriti embed stop")
    time.sleep(0.2)
    assert svc.instance not in services.reasons  # it waits for the request
    gated.release.set()
    t.join(10)
    assert out["got"].vectors == stub(["in flight"])
    assert _wait(lambda: svc.instance in services.reasons, 5)


def test_a_drain_follows_an_index_identity_change(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "a.md").write_text("# A\n\nSome text.\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: StubEmbedding())
    indexer.index_tree(root=root, db=root / ".smriti" / "index.db")
    svc = services.start(root, identity_check_s=0.05)
    client = ServiceEmbedding(root, spawn=False)
    assert client.embed_with_identity(["before"]).identity == STUB

    class Tiny:
        model_name, dimension = "tiny-8", 8

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * 7 for _ in texts]

    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: Tiny())
    indexer.index_tree(full=True, root=root, db=root / ".smriti" / "index.db")

    assert _wait(lambda: svc.instance in services.reasons, 5)
    assert "index now records" in services.reasons[svc.instance]
    assert read_discovery(service_paths(runtime_dir(), root)) is None
    with pytest.raises(EmbeddingUnavailable, match="not running"):
        client.embed_with_identity(["after"])


def test_mixed_client_versions_coexist_and_another_protocol_degrades(
        root: Path, services: Services) -> None:
    svc = services.start(root)
    old = ServiceEmbedding(root, spawn=False, version="0.1.0")
    new = ServiceEmbedding(root, spawn=False, version="0.2.0.dev0")
    assert old.embed_with_identity(["one"]).vectors == stub(["one"])
    assert new.embed_with_identity(["two"]).vectors == stub(["two"])
    assert old.last_instance == new.last_instance == svc.instance

    future = ServiceEmbedding(root, spawn=True, protocol=PROTOCOL + 1)
    spawned: list[object] = []
    future.start_service = lambda paths, **kw: bool(spawned.append(paths))
    with pytest.raises(EmbeddingUnavailable, match="protocol"):
        future.embed_with_identity(["three"])
    assert not spawned  # it neither replaces the service nor starts another
    assert old.embed_with_identity(["four"]).vectors == stub(["four"])
    assert old.last_instance == svc.instance and svc.state == "ready"


def _st(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def test_a_foreign_owned_or_world_writable_runtime_directory_is_refused() -> None:
    d = stat.S_IFDIR
    assert private_dir_problem(_st(d | 0o700, 1000), 1000) is None
    assert private_dir_problem(_st(d | 0o755, 1000), 1000) is None  # tightened, not refused
    assert "owned by uid 0" in str(private_dir_problem(_st(d | 0o700, 0), 1000))
    assert "write" in str(private_dir_problem(_st(d | 0o777, 1000), 1000))
    assert "write" in str(private_dir_problem(_st(d | 0o720, 1000), 1000))
    assert "symbolic link" in str(private_dir_problem(_st(stat.S_IFLNK | 0o700, 1000), 1000))
    assert "not a directory" in str(private_dir_problem(_st(stat.S_IFREG | 0o600, 1000), 1000))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_runtime_dir_checks_ownership_and_modes_on_posix(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "run"
    monkeypatch.setenv("SMRITI_RUNTIME_DIR", str(run))
    assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700
    run.chmod(0o755)
    runtime_dir()
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    run.chmod(0o777)
    with pytest.raises(RuntimeDirError, match="write"):
        runtime_dir()
    run.chmod(0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(RuntimeDirError, match="owned by"):
        runtime_dir()
    monkeypatch.setattr(os, "getuid", lambda: real_uid)
    key = run / "embed.key"
    load_key(run)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    key.chmod(0o644)
    with pytest.raises(RuntimeDirError, match="only this user"):
        load_key(run)


def test_the_key_is_made_once_atomically_and_never_replaced(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    start = threading.Barrier(8)
    keys: list[bytes] = []

    def make() -> None:
        start.wait()
        keys.append(load_key(run))

    threads = [threading.Thread(target=make) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1 and len(keys[0]) == 32
    assert [p.name for p in run.iterdir()] == ["embed.key"]  # no temporaries left
    (run / "embed.key").write_text("not a key", encoding="ascii")
    with pytest.raises(RuntimeDirError, match="malformed"):
        load_key(run)
    assert (run / "embed.key").read_text(encoding="ascii") == "not a key"


def test_a_timed_out_write_followed_only_by_reads_ends_with_nothing_pending(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory) -> None:
    """Round 2, finding 3: vectors left pending by a write that timed out must be
    filled even if no write or index run ever follows, only reads."""
    from smriti.store.schema import open_readonly, pending_vectors

    if not _vec_loads(tmp_path_factory.mktemp("probe")):
        pytest.skip("sqlite-vec can't load here: nothing pending can be filled")
    from smriti.store.search import search_with_status

    (root / "a.md").write_text("# A\n\nSome text.\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: StubEmbedding())
    db = root / ".smriti" / "index.db"
    indexer.index_tree(root=root, db=db)
    gated = Gated()  # the write's embedding takes longer than the write waits
    services.start(root, gated, pending_poll_s=0.05)
    client = ServiceEmbedding(root, spawn=False, write_s=0.3)
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: client)
    note = root / "lantern.md"
    note.write_text("# Lantern\n\nAri hung a brass lantern by the door.\n", encoding="utf-8")

    result = indexer.index_file_result(note, root=root, db=db)
    assert result.chunks and result.vectors_pending  # saved, keyword-searchable, vectors to come
    gated.release.set()

    def pending() -> int:
        conn = open_readonly(db)
        try:
            return pending_vectors(conn)
        finally:
            conn.close()

    deadline = time.monotonic() + 10
    reads = 0
    while time.monotonic() < deadline and pending():
        conn = open_readonly(db)  # only reads, through the same service
        try:
            assert search_with_status(conn, "brass lantern", use_reranker=False).results
            reads += 1
        finally:
            conn.close()
        time.sleep(0.05)
    assert pending() == 0 and reads
    conn = open_readonly(db)
    try:
        outcome = search_with_status(conn, "brass lantern", use_reranker=False)
    finally:
        conn.close()
    assert outcome.keyword_only == "" and outcome.results[0].source == "lantern.md"
    assert outcome.results[0].score > 0.9  # its vector is in: it scores on both legs


def test_a_starting_service_fills_pending_vectors_unasked(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory) -> None:
    from smriti.store.schema import open_readonly, pending_vectors

    if not _vec_loads(tmp_path_factory.mktemp("probe")):
        pytest.skip("sqlite-vec can't load here: nothing pending can be filled")

    class Down(StubEmbedding):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("no embedder")

    db = root / ".smriti" / "index.db"
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: StubEmbedding())
    (root / "a.md").write_text("# A\n\nSome text.\n", encoding="utf-8")
    indexer.index_tree(root=root, db=db)
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: Down())
    for name in ("b", "c"):
        (root / f"{name}.md").write_text(f"# {name}\n\nWritten while down.\n", encoding="utf-8")
        assert indexer.index_file_result(root / f"{name}.md", root=root, db=db).vectors_pending

    def pending() -> int:
        conn = open_readonly(db)
        try:
            return pending_vectors(conn)
        finally:
            conn.close()

    assert pending() == 2
    svc = services.start(root, repair_batch=1)  # one source a step
    assert _wait(lambda: pending() == 0, 10)
    assert svc.stats.repaired == 2 and svc.stats.served == 0


def _vec_loads(tmp: Path) -> bool:
    from smriti.store.schema import ensure_schema

    conn = ensure_schema(tmp / "probe.db", None)
    try:
        return conn.has_vec
    finally:
        conn.close()


def test_without_sqlite_vec_the_repair_loop_waits_and_fills_once_it_loads(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory) -> None:
    """Codex review of H10, P2: a write without sqlite-vec must stay pending; and
    while sqlite-vec won't load, the repair loop must not spin on it."""
    import smriti.store.schema as schema
    from smriti.store.schema import open_readonly, pending_vectors

    if not _vec_loads(tmp_path_factory.mktemp("probe")):
        pytest.skip("sqlite-vec can't load here")
    real_load = schema._load_sqlite_vec
    monkeypatch.setattr(schema, "_load_sqlite_vec", lambda conn: False)
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: StubEmbedding())
    db = root / ".smriti" / "index.db"
    (root / "a.md").write_text("# A\n\nWritten on the base install.\n", encoding="utf-8")
    assert indexer.index_file_result(root / "a.md", root=root, db=db).vectors_pending
    fills: list[object] = []
    real_fill = indexer.fill_pending_vectors

    def counting_fill(**kw: Any) -> int:
        fills.append(kw)
        return real_fill(**kw)

    monkeypatch.setattr(indexer, "fill_pending_vectors", counting_fill)
    svc = services.start(root, pending_poll_s=0.02, novec_poll_s=0.8)
    time.sleep(0.5)  # 25 looks at the 0.02 s poll, had it not backed off
    assert svc.pending == 1 and not svc.can_store_vectors
    assert fills == []  # no repair attempted: no write transaction, no embedding

    monkeypatch.setattr(schema, "_load_sqlite_vec", real_load)  # now it loads

    def pending() -> int:
        conn = open_readonly(db)
        try:
            return pending_vectors(conn)
        finally:
            conn.close()

    assert _wait(lambda: pending() == 0, 10)
    assert svc.can_store_vectors and svc.stats.repaired == 1 and len(fills) >= 1


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def test_a_crashed_service_of_ours_is_reaped_and_replaced_without_knocking(
        root: Path, services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex review of H10, P2: on POSIX a service that crashed stays a zombie
    while the MCP server that started it lives, and a zombie's pid probes alive;
    children were reaped only when starting another, so its stale "ready"
    record was tried twice and nothing replaced it."""
    import smriti.embed.client as client_mod

    paths = service_paths(runtime_dir(), root)
    written = time.time()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # the service, about to crash
    if hasattr(os, "waitid"):
        os.waitid(os.P_PID, dead.pid, os.WEXITED | os.WNOWAIT)  # exited, not reaped
    else:
        assert _wait(lambda: not client_mod.pid_alive(dead.pid), 10)
    real_alive = client_mod.pid_alive
    # A zombie's pid probes alive (POSIX); the same here on every platform.
    monkeypatch.setattr(client_mod, "pid_alive",
                        lambda pid: dead.returncode is None if pid == dead.pid else real_alive(pid))
    write_discovery(paths, Discovery(dead.pid, "ready", "crashed", port=_closed_port(),
                                     identity=STUB, updated=written))
    client = ServiceEmbedding(root)
    client._children.append(dead)
    knocked: list[str] = []
    real_call = client._call

    def recording_call(found: Discovery, *a: Any, **k: Any) -> Any:
        knocked.append(found.instance)
        return real_call(found, *a, **k)

    monkeypatch.setattr(client, "_call", recording_call)
    started: list[EmbedService] = []

    def start(paths: Any, **kw: Any) -> bool:
        started.append(services.start(root))
        return True

    client.start_service = start  # type: ignore[method-assign]

    got = client.embed_with_identity(["after the crash"])

    assert got.vectors == stub(["after the crash"])
    assert dead.returncode == 0  # reaped
    assert "crashed" not in knocked  # known dead before any connection was tried
    assert len(started) == 1 and client.last_instance == started[0].instance


def test_a_dead_service_whose_pid_still_looks_alive_is_replaced_under_its_lock(
        root: Path, services: Services) -> None:
    """Codex review of H10, P2: another session's service died (a zombie of that
    session, or a reused pid): its record says ready and its pid probes alive,
    but nothing listens. Refused twice, the client gave up and nothing replaced
    it; now the lock, free once the service is gone, proves it dead."""
    paths = service_paths(runtime_dir(), root)
    write_discovery(paths, Discovery(os.getpid(), "ready", "crashed", port=_closed_port(),
                                     identity=STUB))
    client = ServiceEmbedding(root)
    throttles: list[object] = []

    def start(paths: Any, **kw: Any) -> bool:
        throttles.append(kw.get("throttle", True))
        services.start(root)
        return True

    client.start_service = start  # type: ignore[method-assign]

    got = client.embed_with_identity(["replaced"])

    assert got.vectors == stub(["replaced"])
    assert throttles == [False]  # one replacement, started past the start throttle
    found = read_discovery(paths)
    assert found is not None and found.instance == client.last_instance != "crashed"


def test_an_unreachable_service_that_holds_its_lock_is_left_alone(root: Path) -> None:
    """The lock is what tells a dead service from a live one that isn't accepting
    (draining, swamped): a held lock means alive, so its record stands."""
    from smriti.core.oslock import exclusive_lock

    paths = service_paths(runtime_dir(), root)
    write_discovery(paths, Discovery(os.getpid(), "ready", "alive-but-busy",
                                     port=_closed_port(), identity=STUB))
    started: list[object] = []
    client = ServiceEmbedding(root, query_s=1.5)
    client.start_service = lambda paths, **kw: bool(started.append(kw))  # type: ignore
    with exclusive_lock(paths.lock, wait_s=1) as held:
        assert held
        t0 = time.monotonic()
        with pytest.raises(EmbeddingUnavailable):
            client.embed_with_identity(["x"], kind="query")
        assert time.monotonic() - t0 < 2.5  # within the query's deadline
    found = read_discovery(paths)
    assert found is not None and found.instance == "alive-but-busy" and started == []


def test_a_replacement_waits_out_a_drain_longer_than_a_second(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex recheck of H10, P2: once the first lock attempt waited 1 s, `if wait_s`
    was true on it, so a replacement gave up instead of waiting for a predecessor
    that took longer than a second to drain."""
    import smriti.embed.service as service
    from smriti.core.oslock import exclusive_lock
    from smriti.embed.runtime import Discovery, service_paths, write_discovery

    paths = service_paths(runtime_dir(), root)
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    served: list[float] = []
    monkeypatch.setattr(service, "_serve_locked",
                        lambda *a, **k: served.append(time.monotonic()) or 0)
    holding, release = threading.Event(), threading.Event()

    def draining_predecessor() -> None:
        with exclusive_lock(paths.lock, wait_s=5) as held:
            assert held
            write_discovery(paths, Discovery(pid=os.getpid(), state="draining",
                                             instance="old"))
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=draining_predecessor)
    holder.start()
    assert holding.wait(5)
    threading.Timer(2.5, release.set).start()  # drains for longer than the first wait
    t0 = time.monotonic()
    try:
        assert service.serve(root) == 0
    finally:
        release.set()
        holder.join(10)
    assert served and served[0] - t0 >= 2.0  # it waited, then took over


def test_stop_reaches_a_service_that_was_still_starting(root: Path) -> None:
    """Codex review of H10, P2: `smriti embed stop` during the model load found the
    service "starting", sent no stop, and only watched the pid: the service came
    up and stayed up."""
    runtime = runtime_dir()
    slow_start = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "import smriti.embed.service as s\n"
        "load = s._load_provider\n"
        "def slow(db):\n"
        "    time.sleep(2.0)  # a model loading\n"
        "    return load(db)\n"
        "s._load_provider = slow\n"
        "sys.exit(s.serve(Path(sys.argv[1]), idle_s=120))\n")
    proc = subprocess.Popen([sys.executable, "-c", slow_start, str(root)],
                            env=_service_env(runtime), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    # Reap the child as soon as it exits, as a real parent would: on POSIX an
    # unreaped child stays a zombie, which counts as alive, and stop_service
    # would wait for it in vain.
    exit_codes: list[int] = []
    reaper = threading.Thread(target=lambda: exit_codes.append(proc.wait()), daemon=True)
    reaper.start()
    try:
        paths = service_paths(runtime, root)
        assert _wait(lambda: (d := read_discovery(paths)) is not None
                     and d.state == "starting", 60)
        t0 = time.monotonic()
        outcome = stop_service(root, wait_s=20)
        assert outcome == f"stopped (pid {proc.pid})", outcome
        reaper.join(10)
        assert exit_codes == [0] and time.monotonic() - t0 < 15
        assert read_discovery(paths) is None
    finally:
        _kill([proc])


def test_a_query_does_not_wait_while_the_service_is_starting(
        root: Path, services: Services) -> None:
    """H11.10: during a cold start (about 30 s of model loading) every query
    waited out its whole 3 s deadline before falling back to keywords."""
    paths = service_paths(runtime_dir(), root)
    # A live service, still loading its model: the record a starting `serve` writes.
    write_discovery(paths, Discovery(os.getpid(), "starting", "warming", root=str(paths.root)))
    query = ServiceEmbedding(root, spawn=False)  # the usual 3 s query deadline

    t0 = time.monotonic()
    with pytest.raises(EmbeddingUnavailable, match="is starting"):
        query.embed_with_identity(["brass lantern"], kind="query")
    assert time.monotonic() - t0 < 1.0  # at once, not after 3 s

    # A write still waits for it, and gets its vectors once the service is up.
    writer = ServiceEmbedding(root, spawn=False, write_s=20)
    up = threading.Timer(0.3, services.start, args=(root,))
    up.start()
    try:
        got = writer.embed_with_identity(["brass lantern"], kind="write")
    finally:
        up.join(10)
    assert got.identity == STUB and got.vectors == stub(["brass lantern"])
    # And a query, once the service is ready, is served as before.
    assert query.embed_with_identity(["brass lantern"], kind="query").identity == STUB


# ── Real processes ───────────────────────────────────────────────────


def _service_env(runtime: Path) -> dict[str, str]:
    path = os.pathsep.join(p for p in (str(SRC), os.environ.get("PYTHONPATH", "")) if p)
    return dict(os.environ, SMRITI_RUNTIME_DIR=str(runtime), SMRITI_EMBED_PROVIDER="stub",
                SMRITI_EMBED_SERVICE="1", PYTHONPATH=path)


def _kill(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(10)


def test_simultaneous_cold_starts_give_one_service(root: Path) -> None:
    runtime = runtime_dir()
    env = _service_env(runtime)
    start = threading.Barrier(4)
    procs: list[subprocess.Popen[bytes]] = []
    lock = threading.Lock()

    def launch() -> None:
        start.wait()
        p = subprocess.Popen([sys.executable, "-m", "smriti", "embed", "serve", "--root",
                              str(root)], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        with lock:
            procs.append(p)

    threads = [threading.Thread(target=launch) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        paths = service_paths(runtime, root)
        assert _wait(lambda: (d := read_discovery(paths)) is not None and d.state == "ready", 60)
        assert _wait(lambda: sum(p.poll() is None for p in procs) == 1, 30)
        [alive] = [p for p in procs if p.poll() is None]
        assert all(p.returncode == 0 for p in procs if p is not alive)
        found = read_discovery(paths)
        assert found is not None and found.pid == alive.pid
        cli = [sys.executable, "-m", "smriti", "embed"]
        status = subprocess.run([*cli, "status", "--root", str(root)], env=env,
                                capture_output=True, text=True, timeout=60)
        assert "ready" in status.stdout and status.returncode == 0
        got = ServiceEmbedding(root, spawn=False).embed_with_identity(["hello"])
        assert got.vectors == stub(["hello"])
        stopped = subprocess.run([*cli, "stop", "--root", str(root)], env=env,
                                 capture_output=True, text=True, timeout=90)
        assert "stopped" in stopped.stdout, stopped
        assert alive.wait(30) == 0
        assert read_discovery(paths) is None
    finally:
        _kill(procs)


def test_clients_starting_a_cold_service_at_once_share_one(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = runtime_dir()
    for key, value in _service_env(runtime).items():
        monkeypatch.setenv(key, value)
    start = threading.Barrier(4)
    clients = [ServiceEmbedding(root) for _ in range(4)]
    out: list[Any] = [None] * 4

    def call(i: int) -> None:
        start.wait()
        try:
            out[i] = clients[i].embed_with_identity([f"cold {i}"])
        except EmbeddingUnavailable as exc:
            out[i] = exc

    threads = [threading.Thread(target=call, args=(i,)) for i in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        assert all(o is not None and not isinstance(o, Exception) for o in out), out
        assert [o.vectors for o in out] == [stub([f"cold {i}"]) for i in range(4)]
        assert len({c.last_instance for c in clients}) == 1
        state = service_state(root)
        assert state.state == "ready", state
    finally:
        print(stop_service(root))
        _kill([p for c in clients for p in c._children])

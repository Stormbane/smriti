"""The embedding service: one model per memory root, shared by every long-lived process.

``smriti embed serve`` loads the index's embedding model once and answers
embedding requests over loopback TCP (see :mod:`smriti.embed.protocol`).

- **Admission is bounded.** At most 32 connections at once; one more is
  closed on accept, before a thread or buffer is spent on it. The handshake
  must finish within 1 s, and requests wait in a bounded queue (64). A
  request whose client has given up is dropped unrun. So a peer that floods
  or stalls costs bounded threads, sockets and memory, and clients degrade
  promptly instead of hanging.
- **An authenticated connection carries many requests.** After a reply the
  service waits up to 10 s (``keepalive_s``, advertised in its hello) for
  the next request on it, one at a time, and closes it then, or at once
  when it drains. It holds its slot while it lives. A client that doesn't
  know keep-alive closes after one request, as before; only the hello is
  sent before authentication, as before.
- **Inference is serialised**, queries ahead of write batches; between
  requests, a repair loop fills the vectors of sources indexed while no
  embedder was available, in small batches, queries first.
- **The index decides restarts.** The service serves the identity it
  loaded; when the index records another (a migration), it drains: stops
  accepting, finishes in-flight work and exits, and the next client starts
  a fresh one. Clients of other versions use whatever service is running.
- **Holds keep it loaded while sessions are open.** A ``hold`` is an
  authenticated request that does no work: it only resets the idle timer,
  and answers with the instance and ``idle_s``. A long-lived client (the
  MCP server) sends one every ``idle_s / 3`` from its first embedding on
  (:mod:`smriti.embed.client`). It is advertised in the hello (``"hold":
  true``); a service from before it answers "unknown op", and the client
  stops holding that instance. A hold never undoes a drain: the drain is
  decided before it, and a draining service accepts nothing new.
- **Idle exit** after 15 minutes without a request or a hold
  (``SMRITI_EMBED_IDLE_S``), so once no session has held it for that long,
  returns the model's memory to the operating system, and lets new code in.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import queue
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from smriti.embed.identity import (
    Embedded,
    EmbeddingIdentity,
    StaleEmbedding,
    identity_of,
    mismatched_identity,
)
from smriti.embed.protocol import (
    MAX_FRAME,
    MAX_TEXTS,
    PREAUTH_MAX,
    PROTOCOL,
    Frame,
    PeerGone,
    ProtocolError,
    client_proof,
    encode_vectors,
    is_nonce,
    proofs_match,
    recv_frame,
    send_frame,
    server_proof,
)
from smriti.embed.runtime import (
    Discovery,
    ServicePaths,
    clear_discovery,
    load_key,
    read_discovery,
    runtime_dir,
    service_paths,
    write_discovery,
)

log = logging.getLogger(__name__)

IDLE_S = 900.0
MAX_CONNECTIONS = 32
QUEUE_MAX = 64
HANDSHAKE_S = 1.0
REQUEST_READ_S = 5.0  # after the handshake, for the request frame to arrive
KEEPALIVE_S = 10.0  # how long an authenticated connection waits for its next request
_KEEPALIVE_POLL_S = 0.1  # how often a waiting connection looks for a drain
MAX_BUDGET_S = 60.0  # the longest deadline a request may ask for
DRAIN_WAIT_S = 45.0  # how long a draining service waits for in-flight connections


class _Provider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class _Job:
    texts: list[str]
    deadline: float  # monotonic
    done: threading.Event = field(default_factory=threading.Event)
    vectors: list[list[float]] | None = None
    error: str = ""
    cancelled: bool = False


@dataclass
class Stats:
    served: int = 0  # embed requests answered with vectors
    refused: int = 0  # connections closed on accept: too many at once
    busy: int = 0  # requests refused because the queue was full
    timed_out: int = 0  # requests answered "timeout": their deadline passed while queued
    dropped: int = 0  # requests whose client gave up before they ran
    auth_failures: int = 0
    repaired: int = 0  # sources whose pending vectors the repair loop filled
    authenticated: int = 0  # connections that passed the handshake
    held: int = 0  # holds answered: requests that only reset the idle timer


class _Interrupted(Exception):
    """The service is stopping: a repair step gives way."""


class EmbedService:
    """The listener, its connection workers, the inference thread and the repair loop."""

    def __init__(self, *, provider: _Provider, key: bytes, db: Path | None,
                 identity: EmbeddingIdentity | None = None, idle_s: float = IDLE_S,
                 handshake_s: float = HANDSHAKE_S, request_read_s: float = REQUEST_READ_S,
                 keepalive_s: float = KEEPALIVE_S, hold: bool = True,
                 max_connections: int = MAX_CONNECTIONS, queue_max: int = QUEUE_MAX,
                 identity_check_s: float = 1.0, pending_poll_s: float = 2.0,
                 novec_poll_s: float = 60.0,
                 repair_batch: int = 16, instance: str | None = None,
                 on_state: Callable[[str], None] | None = None,
                 on_drain: Callable[[str], None] | None = None) -> None:
        self.provider = provider
        self.identity = identity or identity_of(provider)
        self.key = key
        self.db = db
        self.idle_s = idle_s
        self.handshake_s = handshake_s
        self.request_read_s = request_read_s
        self.keepalive_s = keepalive_s  # 0: one request per connection, as before keep-alive
        self.hold = hold  # False: no "hold" op, as before it (an older service, in tests)
        self.identity_check_s = identity_check_s
        self.pending_poll_s = pending_poll_s
        self.novec_poll_s = novec_poll_s  # how often to look again while sqlite-vec won't load
        self.can_store_vectors = True
        self.repair_batch = repair_batch
        self.instance = instance or secrets.token_hex(8)
        self.port = 0
        self.stats = Stats()
        self.pending = 0  # pending sources at the last look
        self.state = "starting"
        self._on_state = on_state
        self._on_drain = on_drain
        self._slots = threading.BoundedSemaphore(max_connections)
        self._active = 0
        self._active_lock = threading.Lock()
        self._queue: queue.PriorityQueue[tuple[int, int, _Job]] = queue.PriorityQueue(queue_max)
        self._seq = itertools.count()
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._stop_reason = ""
        self._draining = threading.Event()
        self._stopping = threading.Event()  # the inference thread's signal to end
        self._started = time.monotonic()
        self._last_request = time.monotonic()
        self._next_pending_check = 0.0
        self._threads: list[threading.Thread] = []

    # ── Lifecycle ────────────────────────────────────────────────────

    def bind(self) -> int:
        """Listen on an ephemeral loopback port; returns it."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Without it another process could bind the same port with
            # SO_REUSEADDR and take connections meant for this one.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        return self.port

    def stop(self, reason: str = "stopped") -> None:
        """Ask the service to drain and exit (from any thread)."""
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()

    @property
    def active_connections(self) -> int:
        return self._active

    def run(self) -> str:
        """Serve until stopped, idle or the index changes identity; returns why it ended."""
        if self._listener is None:
            self.bind()
        self._set_state("ready")
        for target, name in ((self._accept_loop, "accept"), (self._infer_loop, "infer")):
            t = threading.Thread(target=target, name=f"smriti-embed-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        last_identity_check = 0.0
        reason = ""
        while not reason:
            if self._stop.wait(0.05):
                reason = self._stop_reason or "stopped"
                break
            now = time.monotonic()
            if (self.idle_s and now - self._last_request >= self.idle_s
                    and self._active == 0 and self._queue.empty()):
                reason = f"idle for {self.idle_s:.0f}s"
            elif now - last_identity_check >= self.identity_check_s:
                last_identity_check = now
                changed = self._index_identity_changed()
                if changed is not None:
                    reason = f"the index now records {changed.describe()}"
        self._drain(reason)
        return reason

    def _set_state(self, state: str) -> None:
        """Publish ``state`` (the discovery file), then take it on."""
        if self._on_state is not None:
            try:
                self._on_state(state)
            except OSError as exc:  # a discovery file it can't write: clients wait it out
                log.warning("could not publish the service state %r: %s", state, exc)
        self.state = state

    def _drain(self, reason: str) -> None:
        log.info("embedding service draining: %s", reason)
        # Published before anything is refused: a client whose connection is
        # closed from here on finds "draining" when it looks again, and starts
        # a fresh service rather than knock on this one twice.
        self._set_state("draining")
        self._draining.set()
        if self._on_drain is not None:
            self._on_drain(reason)
        if self._listener is not None:
            self._listener.close()
        deadline = time.monotonic() + DRAIN_WAIT_S
        while self._active and time.monotonic() < deadline:
            time.sleep(0.02)
        self._stopping.set()
        for t in self._threads:
            t.join(timeout=5.0)

    def _index_identity_changed(self) -> EmbeddingIdentity | None:
        if self.db is None or not self.db.exists():
            return None
        import sqlite3

        from smriti.store.schema import open_readonly

        try:
            conn = open_readonly(self.db)
        except (sqlite3.Error, OSError) as exc:
            log.warning("could not read the index's embedding identity: %s", exc)
            return None
        try:
            return mismatched_identity(conn, self.identity)
        except sqlite3.Error as exc:
            log.warning("could not read the index's embedding identity: %s", exc)
            return None
        finally:
            conn.close()

    # ── Connections ──────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._draining.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break  # closed by the drain
            if self._draining.is_set() or not self._slots.acquire(blocking=False):
                self.stats.refused += 1
                conn.close()
                continue
            with self._active_lock:
                self._active += 1
            threading.Thread(target=self._serve_conn, args=(conn, time.monotonic()),
                             name="smriti-embed-conn", daemon=True).start()

    def _serve_conn(self, conn: socket.socket, accepted: float) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if not self._handshake(conn, accepted + self.handshake_s):
                return
            with self._active_lock:
                self.stats.authenticated += 1
            while True:
                request = recv_frame(conn, time.monotonic() + self.request_read_s, MAX_FRAME)
                send_frame(conn, self._handle(request), time.monotonic() + 5.0)
                if request.get("op") == "stop" or not self._next_request(conn):
                    return
        except (TimeoutError, PeerGone, ProtocolError, OSError) as exc:
            log.debug("embedding connection ended: %s", exc)
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            with self._active_lock:
                self._active -= 1
            self._slots.release()

    def _next_request(self, conn: socket.socket) -> bool:
        """Wait for the client's next request on ``conn``: True once it starts to arrive.

        Readable also means closed: the read that follows finds the end. False,
        and the connection is closed, when keep-alive is off, when nothing
        arrives within ``keepalive_s``, or when the service stops or drains (a
        client whose request then finds the connection closed sends it again on
        a fresh one, and finds the service draining).
        """
        if self.keepalive_s <= 0:
            return False
        deadline = time.monotonic() + self.keepalive_s
        while not (self._stop.is_set() or self._draining.is_set()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            conn.settimeout(min(remaining, _KEEPALIVE_POLL_S))
            try:
                conn.recv(1, socket.MSG_PEEK)  # a byte, or b"" at the end: either way, read on
            except TimeoutError:
                continue
            return True
        return False

    def _handshake(self, conn: socket.socket, deadline: float) -> bool:
        hello = recv_frame(conn, deadline, PREAUTH_MAX)
        nonce_c = hello.get("nonce")
        if hello.get("op") != "hello" or not is_nonce(nonce_c):
            return False
        assert isinstance(nonce_c, str)
        if hello.get("proto") != PROTOCOL:
            # Say which protocol this is, so the client degrades knowingly; never more.
            send_frame(conn, {"proto": PROTOCOL, "ok": False, "code": "protocol",
                              "error": f"this service speaks protocol {PROTOCOL}"}, deadline)
            return False
        nonce_s = secrets.token_hex(16)
        answer: Frame = {"proto": PROTOCOL, "instance": self.instance, "nonce": nonce_s,
                         "proof": server_proof(self.key, nonce_c, nonce_s, self.instance)}
        if self.keepalive_s > 0:
            answer["keepalive_s"] = self.keepalive_s
        if self.hold:
            answer["hold"] = True  # a constant: nothing new is read before authentication
        send_frame(conn, answer, deadline)
        auth = recv_frame(conn, deadline, PREAUTH_MAX)
        if auth.get("op") != "auth" or not proofs_match(
                auth.get("proof"), client_proof(self.key, nonce_c, nonce_s)):
            self.stats.auth_failures += 1
            log.warning("refused an embedding client that failed authentication")
            return False
        return True

    def _handle(self, request: Frame) -> Frame:
        op = request.get("op")
        if op == "embed":
            return self._handle_embed(request)
        if op == "status":
            return {"ok": True, **self.status()}
        if op == "hold" and self.hold:
            return self._handle_hold()
        if op == "stop":
            self.stop("stopped on request")
            return {"ok": True, "instance": self.instance}
        return {"ok": False, "code": "op", "error": f"unknown op {op!r}"}

    def _handle_hold(self) -> Frame:
        """Reset the idle timer, and nothing else: a session is open that will search again.

        A draining service is past its idle check (``run`` decides a drain
        once), so a hold that reaches it changes nothing; its answer says
        "draining", and the client follows the discovery file to the next.
        """
        if not self._draining.is_set():
            self._last_request = time.monotonic()
        with self._active_lock:
            self.stats.held += 1
        return {"ok": True, "instance": self.instance, "state": self.state,
                "idle_s": self.idle_s}

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {"instance": self.instance, "pid": os.getpid(), "state": self.state,
                "identity": asdict(self.identity), "uptime_s": round(now - self._started, 1),
                "idle_for_s": round(now - self._last_request, 1), "idle_s": self.idle_s,
                "keepalive_s": self.keepalive_s, "hold": self.hold,
                "queued": self._queue.qsize(), "active": self._active,
                "pending": self.pending, "can_store_vectors": self.can_store_vectors,
                "stats": asdict(self.stats)}

    def _handle_embed(self, request: Frame) -> Frame:
        texts, kind, budget = request.get("texts"), request.get("kind"), request.get("budget_ms")
        if (not isinstance(texts, list) or not all(isinstance(t, str) for t in texts)
                or not texts or len(texts) > MAX_TEXTS):
            return {"ok": False, "code": "request",
                    "error": f"texts must be 1 to {MAX_TEXTS} strings"}
        if kind not in ("query", "write") or not isinstance(budget, int) or budget <= 0:
            return {"ok": False, "code": "request", "error": "bad kind or budget"}
        self._last_request = time.monotonic()
        job = _Job(list(texts), time.monotonic() + min(budget / 1000.0, MAX_BUDGET_S))
        try:
            self._queue.put_nowait((0 if kind == "query" else 1, next(self._seq), job))
        except queue.Full:
            self.stats.busy += 1
            return {"ok": False, "code": "busy", "error": "the embedding queue is full"}
        if not job.done.wait(max(0.0, job.deadline - time.monotonic())):
            job.cancelled = True
            self.stats.timed_out += 1
            return {"ok": False, "code": "timeout", "error": "the request's deadline passed"}
        self._last_request = time.monotonic()
        if job.vectors is None:
            return {"ok": False, "code": "failed", "error": job.error or "embedding failed"}
        self.stats.served += 1
        return {"ok": True, "instance": self.instance, "identity": asdict(self.identity),
                "count": len(job.vectors), "dim": self.identity.dimension,
                "vectors": encode_vectors(job.vectors)}

    # ── Inference ────────────────────────────────────────────────────

    def _infer_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            while not self._stopping.is_set():
                try:
                    _, _, job = self._queue.get(timeout=0.05)
                except queue.Empty:
                    self._maybe_repair()
                    continue
                self._run_job(job)
        finally:
            loop.close()

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._loop.run_until_complete(self.provider.embed(texts))

    def _run_job(self, job: _Job) -> None:
        if job.cancelled or time.monotonic() >= job.deadline:
            self.stats.dropped += 1
            job.error = "expired"
            job.done.set()
            return
        try:
            vectors = self._embed(job.texts)
            if len(vectors) != len(job.texts):
                raise ValueError(f"{len(vectors)} vectors for {len(job.texts)} texts")
            job.vectors = [list(map(float, v)) for v in vectors]
        except Exception as exc:  # noqa: BLE001 — returned to the client, and logged
            log.warning("embedding failed: %s", exc)
            job.error = f"{type(exc).__name__}: {exc}"
        job.done.set()

    def _serve_waiting(self) -> None:
        """Run whatever requests are waiting, queries first (the queue's order)."""
        while not self._stopping.is_set():
            try:
                _, _, job = self._queue.get_nowait()
            except queue.Empty:
                return
            self._run_job(job)
        raise _Interrupted

    def _pending_count(self) -> tuple[int, bool]:
        """Pending sources, and whether this process can store vectors (sqlite-vec loads)."""
        import sqlite3

        from smriti.store.schema import open_readonly, pending_vectors

        assert self.db is not None
        try:
            conn = open_readonly(self.db)
        except (sqlite3.Error, OSError) as exc:
            log.debug("could not read the pending count: %s", exc)
            return 0, True
        try:
            return pending_vectors(conn), conn.has_vec
        finally:
            conn.close()

    def _maybe_repair(self) -> None:
        """One small repair step, when sources are pending and nothing else is waiting."""
        if self._draining.is_set() or self.db is None or not self.db.exists():
            return
        now = time.monotonic()
        if now < self._next_pending_check:
            return
        self._next_pending_check = now + self.pending_poll_s
        self.pending, self.can_store_vectors = self._pending_count()
        if not self.can_store_vectors:
            # Nothing pending can be filled while sqlite-vec won't load: look
            # again rarely, and don't spend a write or an embedding on it.
            self._next_pending_check = now + self.novec_poll_s
            return
        if not self.pending:
            return
        import sqlite3

        from smriti.store.indexer import fill_pending_vectors

        try:
            filled = fill_pending_vectors(db=self.db, embed=self._repair_embed,
                                          max_chunks=self.repair_batch, busy_ms=1000)
        except _Interrupted:
            return
        except StaleEmbedding as exc:
            self.stop(f"the index's embedding identity changed: {exc}")
            return
        except sqlite3.OperationalError as exc:  # the index is busy: try again later
            log.info("pending-vector repair waits: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001 — logged; the next look tries again
            log.warning("pending-vector repair failed: %s", exc)
            return
        self.stats.repaired += filled
        self.pending = max(0, self.pending - filled)
        if filled and self.pending:
            self._next_pending_check = now  # more to do: carry on between requests

    def _repair_embed(self, texts: list[str]) -> Embedded:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.repair_batch):
            self._serve_waiting()  # requests go first
            vectors += self._embed(texts[i:i + self.repair_batch])
        return Embedded([list(map(float, v)) for v in vectors], self.identity)


# ── `smriti embed serve` ────────────────────────────────────────────


def _load_provider(db: Path) -> _Provider:
    """The model this service serves ``db`` with, by the one rule (``provider_keys``):
    the index's recorded provider, else ``SMRITI_EMBED_PROVIDER``, else the auto order.

    Never a rebuild: a service serves the index as it is.
    """
    from smriti.embed.provider import load_provider, provider_keys

    return load_provider(provider_keys(db))


def idle_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("SMRITI_EMBED_IDLE_S", "") or IDLE_S))
    except ValueError:
        return IDLE_S


def serve(root: Path, *, idle_s: float | None = None) -> int:
    """Run the service for ``root`` in this process until it stops; the exit code.

    A second starter for the same root loses the lock and returns 0 within
    a second, unless the holder is draining: then it waits for the lock, so
    a restart after a drain takes one start, not a race.
    """
    from smriti.core.oslock import exclusive_lock

    runtime = runtime_dir()
    key = load_key(runtime)
    paths = service_paths(runtime, root)
    # The first wait is short but not zero: a client recovering from a dead
    # service holds the lock for a moment (client._recover), and a starter
    # must not lose to that and leave no service at all.
    # The second, longer attempt is only for waiting out a draining holder.
    for attempt, wait_s in enumerate((1.0, DRAIN_WAIT_S + 5.0)):
        with exclusive_lock(paths.lock, wait_s=wait_s) as held:
            if held:
                return _serve_locked(paths, key, idle_s)
        current = read_discovery(paths)
        if attempt > 0 or current is None or current.state != "draining":
            log.info("an embedding service for %s is already running", paths.root)
            return 0
    return 0


def _serve_locked(paths: ServicePaths, key: bytes, idle_s: float | None) -> int:
    from smriti import __version__

    db = paths.root / ".smriti" / "index.db"
    instance = secrets.token_hex(8)
    pid = os.getpid()
    root = str(paths.root)

    def publish(state: str, *, port: int = 0, identity: EmbeddingIdentity | None = None,
                error: str = "") -> None:
        write_discovery(paths, Discovery(pid=pid, state=state, instance=instance, port=port,
                                         root=root, identity=identity, error=error,
                                         updated=time.time(), version=__version__))

    publish("starting")
    try:
        provider = _load_provider(db)
        identity = identity_of(provider)
        changed = None
        if db.exists():
            from smriti.store.schema import open_readonly

            conn = open_readonly(db)
            try:
                changed = mismatched_identity(conn, identity)
            finally:
                conn.close()
        if changed is not None:
            raise RuntimeError(f"the index records {changed.describe()}; this service "
                               f"loaded {identity.describe()}")
    except Exception as exc:  # noqa: BLE001 — published where clients read it, and logged
        log.error("embedding service failed to start: %s", exc)
        publish("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    svc = EmbedService(provider=provider, identity=identity, key=key, db=db,
                       idle_s=idle_seconds() if idle_s is None else idle_s, instance=instance)
    port = svc.bind()
    svc._on_state = lambda state: publish(state, port=port, identity=identity)
    log.info("embedding service %s for %s on port %d: %s", instance, root, port,
             identity.describe())
    try:
        reason = svc.run()
    finally:
        clear_discovery(paths, instance)
    log.info("embedding service %s stopped: %s", instance, reason)
    return 0

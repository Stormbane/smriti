"""The embedding service's client: an embedding provider that holds no model.

:class:`ServiceEmbedding` asks the memory root's service for vectors. When
none is running it starts ``python -m smriti embed serve`` (a hidden
console on Windows, never a detached process) and waits, bounded; a query
never waits while the service is still starting (loading its model). Every
step works to one deadline, from connecting to the last byte of the
answer: 3 s for a query, 30 s for a write's batch. When the deadline
passes, or the service can't be trusted (it fails authentication, or
speaks another protocol), the call raises :class:`EmbeddingUnavailable`
with the reason, and the caller degrades: search by keyword only, a write
with its vectors pending. A connection that closes before its answer
(the service exiting as the request arrived) is retried once, fresh.

The connection is kept for the next request while the service advertises
keep-alive (see :mod:`smriti.embed.protocol`), so the connect and the
handshake are paid once rather than per request (the connect alone takes
14-21 ms on Windows). It is reused only for ``embed`` and ``status``, which
are idempotent: when the service had closed it first, the request goes
once more on a fresh connection. A ``stop`` always goes on its own.

A long-lived process (the MCP server, through
:func:`smriti.embed.hold_while_running`) also *holds* the service, so the
model stays loaded while its session is open, as it did when each server
loaded its own: from the client's first successful embedding on, a daemon
thread sends a ``hold`` at once and then every ``idle_s / 3`` (the service's
own idle exit, from its answer), each on a connection of its own, so a hold
never takes the one kept for requests. When a hold is due and the service
is gone or draining, the thread starts the next one (throttled like any
start), so the model is warm again before the next search. A service from
before holds answers "unknown op": that instance isn't held again, and the
next one is. ``smriti read``, hooks and the nightly don't hold.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Imported where they are used: `smriti status` asks only the discovery
    # file when no service runs, and needs neither (nor secrets).
    import socket
    import subprocess

from smriti.embed import holding
from smriti.embed.identity import Embedded, EmbeddingIdentity, EmbeddingUnavailable
from smriti.embed.protocol import (
    MAX_FRAME,
    MAX_TEXTS,
    PREAUTH_MAX,
    PROTOCOL,
    Frame,
    PeerGone,
    ProtocolError,
    client_proof,
    decode_vectors,
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
    pid_alive,
    read_discovery,
    runtime_dir,
    service_paths,
)

log = logging.getLogger(__name__)

QUERY_DEADLINE_S = 3.0
WRITE_DEADLINE_S = 30.0
SPAWN_BACKOFF_S = 10.0  # one start per root in this long, whoever asks
CONNECT_S = 0.5  # a loopback connect that takes longer finds nobody listening
FAILED_BACKOFF_S = 30.0  # after a failed start, report it this long before trying again
KEEPALIVE_MARGIN_S = 1.0  # a kept connection is reused only this long before the service's close
HOLD_EVERY_S = 300.0  # between holds until the service says its idle_s (the default 900 s / 3)
HOLD_MIN_S = 0.05  # the shortest interval between holds, whatever the service says
HOLD_DEADLINE_S = 2.0  # for one hold, from connecting to its answer
# While a service comes up (started, starting, not yet answering) the next hold
# comes soon, doubling to the cap, so the new instance is held, and its idle_s
# learnt, before it can idle out (Codex review of H12.0b).
HOLD_RETRY_S = 2.0
HOLD_RETRY_MAX_S = 30.0
_HOLD_COMING_UP = ("gone", "starting", "took no connection", "went away", "unavailable")
_REUSABLE = frozenset({"embed", "status"})  # idempotent: safe to send again on a fresh connection
_BATCH_BYTES = MAX_FRAME - 64 * 1024  # room for the frame's other fields


def service_enabled() -> bool:
    """``SMRITI_EMBED_SERVICE=0`` (or false/no/off) turns the service off."""
    value = os.environ.get("SMRITI_EMBED_SERVICE", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


class ServiceRefused(EmbeddingUnavailable):
    """The service answered, and refused: ``code`` says why ("op" for an op it doesn't know)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class _Gone(Exception):
    """The connection ended before an answer: worth one fresh try."""


class _Unreachable(_Gone):
    """The endpoint took no connection (refused, or no answer to connect): perhaps dead."""


class _Stale(Exception):
    """A kept connection the service had closed before it read the request: send it afresh."""


def _peer_closed(sock: socket.socket) -> bool:
    """Whether an idle connection has something to read: only its end, since the service
    sends nothing unasked."""
    import socket

    try:
        sock.settimeout(0.0)  # every later read and write sets its own deadline
        sock.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return False  # nothing to read: open and idle
    except OSError:
        return True
    return True  # its end, or bytes nobody asked for: not a connection to reuse


def _keepalive(hello: Frame) -> float:
    """The keep-alive a service's hello advertises, in seconds; 0 for none (an older one)."""
    value = hello.get("keepalive_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


class _Conn:
    """One authenticated connection to one service instance."""

    def __init__(self, sock: socket.socket, where: tuple[str, str, int],
                 keepalive_s: float) -> None:
        self.sock = sock
        self.where = where  # (discovery file, instance, port): the service it reaches
        self.keepalive_s = keepalive_s  # what the service's hello advertised; 0 for none
        self.pid = os.getpid()
        self.idle_since = time.monotonic()
        # Closes the socket when the connection is dropped unclosed, before the
        # socket's own finaliser would warn, even inside a reference cycle.
        self._closer = weakref.finalize(self, _close_quietly, sock)

    def reusable(self, where: tuple[str, str, int]) -> bool:
        """Whether the next request for the service at ``where`` may go on this connection."""
        return (self.where == where and self.pid == os.getpid()
                and time.monotonic() - self.idle_since < self.keepalive_s - KEEPALIVE_MARGIN_S
                and not _peer_closed(self.sock))

    def close(self) -> None:
        self._closer()


def _batches(texts: list[str]) -> list[list[str]]:
    """``texts`` in requests of at most MAX_TEXTS texts and one frame's bytes."""
    out: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for text in texts:
        n = len(json.dumps(text))
        if n > _BATCH_BYTES:
            raise EmbeddingUnavailable(f"a text of {n} bytes is too large to send for embedding")
        if batch and (len(batch) == MAX_TEXTS or size + n > _BATCH_BYTES):
            out.append(batch)
            batch, size = [], 0
        batch.append(text)
        size += n + 1
    if batch:
        out.append(batch)
    return out


class ServiceEmbedding:
    """Vectors from the memory root's embedding service."""

    def __init__(self, root: Path, *, query_s: float = QUERY_DEADLINE_S,
                 write_s: float = WRITE_DEADLINE_S, spawn: bool = True,
                 protocol: int = PROTOCOL, version: str | None = None,
                 hold: bool | None = None) -> None:
        """``hold``: whether this client holds the service from its first embedding on;
        None (the default) follows the process (:func:`smriti.embed.hold_while_running`)."""
        from smriti import __version__

        self.root = root
        self.query_s = query_s
        self.write_s = write_s
        self.spawn = spawn
        self.protocol = protocol
        self.version = version or __version__
        self.last_identity: EmbeddingIdentity | None = None
        self.last_instance = ""
        self._children: list[subprocess.Popen[bytes]] = []
        self._exited: dict[int, float] = {}  # pid of a service this started -> when seen gone
        self._kept: _Conn | None = None  # the connection kept for the next request
        self._kept_lock = threading.Lock()
        self._service_paths: dict[str, ServicePaths] = {}  # by runtime directory
        self._children_lock = threading.Lock()  # the hold thread starts and reaps services too
        self.hold = hold
        self._holder: threading.Thread | None = None
        self._hold_stop = threading.Event()
        self._hold_lock = threading.Lock()
        self._hold_every = HOLD_EVERY_S
        self._unheld = ""  # the instance that answered it can't be held: not asked again

    def close(self) -> None:
        """Stop holding the service, and close the connection kept for the next request."""
        self.stop_holding()
        with self._kept_lock:
            conn, self._kept = self._kept, None
        if conn is not None:
            conn.close()

    def _take_kept(self, where: tuple[str, str, int]) -> _Conn | None:
        """The kept connection, when the next request for ``where`` may go on it.

        It is taken out under the lock, so two calls never share one: a call
        made while another is in flight opens its own.
        """
        with self._kept_lock:
            conn, self._kept = self._kept, None
        if conn is None or conn.reusable(where):
            return conn
        conn.close()
        return None

    def _keep(self, conn: _Conn) -> None:
        """Keep ``conn`` for the next request, if its service allows that long enough."""
        if conn.keepalive_s <= KEEPALIVE_MARGIN_S:
            conn.close()
            return
        conn.idle_since = time.monotonic()
        with self._kept_lock:
            old, self._kept = self._kept, conn
        if old is not None:
            old.close()

    def _reap(self) -> None:
        """Collect the services this process started that have exited.

        On POSIX an exited child stays a zombie until its parent waits for
        it, and a zombie's pid still probes as alive; polling the handle reaps
        it. Its pid is remembered with the time, so a discovery record it left
        is known to be dead even where the pid is later reused.
        """
        now = time.time()
        with self._children_lock:
            for child in list(self._children):
                if child.poll() is not None:
                    self._children.remove(child)
                    self._exited[child.pid] = now

    def _kept_reaches(self, paths: ServicePaths, found: Discovery) -> bool:
        """Whether the kept connection, still within its keep-alive, is to ``found``.

        That service answered on it moments ago, so its pid needn't be probed
        (with psutil on Windows the probe takes 4 ms, on every request). If it
        has died since, using the connection finds it closed, a fresh connect
        is refused, and it is replaced as any dead service is. Only reads: the
        connection may be another thread's to use.
        """
        conn = self._kept
        return (conn is not None
                and conn.where == (str(paths.discovery), found.instance, found.port)
                and time.monotonic() - conn.idle_since < conn.keepalive_s - KEEPALIVE_MARGIN_S)

    def _alive(self, found: Discovery) -> bool:
        gone = self._exited.get(found.pid)
        if gone is not None and found.updated <= gone:
            return False  # written by a service of ours that has since exited
        return pid_alive(found.pid)

    def _paths(self) -> tuple[Path, ServicePaths]:
        """The runtime directory and this root's files in it.

        The files are worked out once per runtime directory: resolving the
        root took 0.6 ms of every request on Windows.
        """
        runtime = runtime_dir()
        paths = self._service_paths.get(str(runtime))
        if paths is None:
            paths = self._service_paths[str(runtime)] = service_paths(runtime, self.root)
        return runtime, paths

    # The provider interface. The model is the service's: these are what it last said.
    @property
    def model_name(self) -> str:
        return self.last_identity.model if self.last_identity else ""

    @property
    def dimension(self) -> int:
        return self.last_identity.dimension if self.last_identity else 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_with_identity(texts, kind="write").vectors

    def embed_with_identity(self, texts: list[str], *, kind: str = "write") -> Embedded:
        if not texts:
            return Embedded([], self.last_identity or EmbeddingIdentity("", "", "", 0))
        deadline = time.monotonic() + (self.query_s if kind == "query" else self.write_s)
        vectors: list[list[float]] = []
        identity: EmbeddingIdentity | None = None
        for batch in _batches(texts):
            got = self._request_batch(batch, kind, deadline)
            if identity is not None and got.identity != identity:
                raise EmbeddingUnavailable("the embedding service changed models mid-batch")
            identity = got.identity
            vectors += got.vectors
        assert identity is not None
        self.last_identity = identity
        if self._holder is None and (self.hold if self.hold is not None else holding()):
            self._start_holding()
        return Embedded(vectors, identity)

    # ── Holding the service while this process runs ────────────────

    def _start_holding(self) -> None:
        """Start the hold thread, unless it runs. It keeps only a weak reference to the
        client: a client dropped unclosed stops it, and is never kept alive by it."""
        with self._hold_lock:
            if self._holder is not None:
                return
            stop = self._hold_stop = threading.Event()
            self._holder = threading.Thread(target=_hold_loop, args=(weakref.ref(self), stop),
                                            name="smriti-embed-hold", daemon=True)
            weakref.finalize(self, stop.set)
            self._holder.start()

    def stop_holding(self) -> None:
        """Stop the hold thread, if one runs; a later embedding starts it again."""
        with self._hold_lock:
            holder, self._holder = self._holder, None
            self._hold_stop.set()
        if holder is not None and holder is not threading.current_thread():
            holder.join(HOLD_DEADLINE_S + CONNECT_S + 1.0)

    @property
    def is_holding(self) -> bool:
        """Whether the hold thread runs."""
        holder = self._holder
        return holder is not None and holder.is_alive()

    def hold_once(self) -> str:
        """One hold: reset the service's idle timer, or start it if it is gone; what happened.

        It follows the discovery file as a request does: a service that has
        drained or died is replaced, with one start at most, throttled like
        any other, so the model is loaded again before the next search needs
        it. A service still starting is left to load. One that failed to
        start is left to the next search or write, which retries it after
        its backoff and says why: a hold mustn't retry a broken model load
        every few minutes in the background.
        """
        runtime, paths = self._paths()
        self._reap()
        found = read_discovery(paths)
        if found is not None and found.state == "failed":
            return "failed to start; left to the next request"
        alive = found is not None and (self._kept_reaches(paths, found) or self._alive(found))
        if found is None or not alive or found.state == "draining":
            if self.spawn and self.start_service(paths):
                return "gone; started another"
            return "gone"
        if found.state != "ready" or not found.port:
            return found.state
        if found.instance == self._unheld:
            return "this instance can't be held"
        try:
            reply = self._call(found, load_key(runtime), paths,
                               time.monotonic() + HOLD_DEADLINE_S, {"op": "hold"})
        except _Unreachable as exc:
            self._recover(paths, found)
            return f"took no connection: {exc}"
        except _Gone as exc:
            return f"went away: {exc}"
        except ServiceRefused as exc:
            if exc.code == "op":
                # An older service. Not an error: it idles out as it always did.
                self._unheld = found.instance
                log.debug("the embedding service %s can't be held (%s); not holding it",
                          found.instance, exc)
            return f"refused: {exc}"
        except (EmbeddingUnavailable, OSError) as exc:
            return f"unavailable: {exc}"
        idle = reply.get("idle_s")
        if isinstance(idle, (int, float)) and not isinstance(idle, bool) and math.isfinite(idle):
            # Idle exit off (0): nothing to hold off, but a gone service is still restarted.
            self._hold_every = max(HOLD_MIN_S, idle / 3) if idle > 0 else HOLD_EVERY_S
        return "held"

    # ── One request ──────────────────────────────────────────────────

    def _request_batch(self, batch: list[str], kind: str, deadline: float) -> Embedded:
        # Why the last attempt failed, as text: the exception itself would hold
        # this frame through its traceback, and so the client and its kept
        # connection (a service slot) until the garbage collector ran.
        gone = ""
        for _attempt in range(2):
            runtime, paths = self._paths()
            key = load_key(runtime)
            found = self._ready_service(paths, deadline, kind)
            try:
                reply = self._call(found, key, paths, deadline, {
                    "op": "embed", "kind": kind, "texts": batch,
                    "budget_ms": max(1, int((deadline - time.monotonic()) * 1000))})
            except _Unreachable as exc:
                gone = str(exc)
                log.info("the embedding service on port %d took no connection (%s)",
                         found.port, exc)
                self._recover(paths, found)
                continue
            except _Gone as exc:
                gone = str(exc)
                log.info("the embedding service went away mid-request (%s); trying once more",
                         exc)
                continue
            identity = EmbeddingIdentity.from_dict(reply.get("identity"))
            if identity is None or not identity.dimension:
                raise EmbeddingUnavailable("the embedding service sent no identity")
            try:
                vectors = decode_vectors(reply.get("vectors"), len(batch), identity.dimension)
            except ProtocolError as exc:
                raise EmbeddingUnavailable(f"bad vectors from the embedding service: {exc}") \
                    from exc
            self.last_instance = str(reply.get("instance") or "")
            return Embedded(vectors, identity)
        raise EmbeddingUnavailable(f"the embedding service closed the connection twice ({gone})")

    def _ready_service(self, paths: ServicePaths, deadline: float,
                       kind: str = "write") -> Discovery:
        """The ready service's discovery record, starting one if needed, by ``deadline``.

        A query that finds the service still starting doesn't wait for it:
        loading the model takes far longer than a query's 3 s, so every search
        during a cold start used to spend all of them before falling back to
        keywords. It falls back at once instead; a query that starts the
        service waits only until the service says it is starting. A write
        still waits, up to its 30 s.
        """
        spawned = False
        delay = 0.02
        while True:
            self._reap()  # before any liveness check: a dead child must not look alive
            found = read_discovery(paths)
            alive = found is not None and (self._kept_reaches(paths, found)
                                           or self._alive(found))
            if found is not None and alive and found.state == "ready" and found.port:
                return found
            if (found is not None and found.state == "failed"
                    and time.time() - found.updated < FAILED_BACKOFF_S):
                raise EmbeddingUnavailable(f"the embedding service failed to start: {found.error}")
            starting = found is not None and alive and found.state == "starting"
            if starting and kind == "query":
                raise EmbeddingUnavailable(
                    "the embedding service is starting (loading its model); try again shortly")
            if not starting and not spawned:
                if not self.spawn:
                    raise EmbeddingUnavailable("the embedding service is not running")
                self.start_service(paths)
                spawned = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmbeddingUnavailable("the embedding service is starting; try again shortly")
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.25)

    def _call(self, found: Discovery, key: bytes, paths: ServicePaths, deadline: float,
              request: Frame) -> Frame:
        """Send ``request`` to the service in ``found``; its reply.

        An ``embed`` or ``status`` goes on the connection kept from the last
        call when the service allows it, and once more on a fresh connection
        if the service had closed that one first. Anything else goes on a
        fresh connection, authenticated both ways, and is sent only once.
        """
        where = (str(paths.discovery), found.instance, found.port)
        reusable = request.get("op") in _REUSABLE
        kept = self._take_kept(where) if reusable else None
        if kept is not None:
            try:
                return self._answer(kept, self._exchange(kept, request, deadline, kept=True),
                                    keep=True)
            except _Stale as exc:
                log.debug("the embedding service had closed the kept connection (%s); "
                          "sending again on a fresh one", exc)
        conn = self._connect(found, key, paths, deadline, where)
        return self._answer(conn, self._exchange(conn, request, deadline, kept=False),
                            keep=reusable)

    def _answer(self, conn: _Conn, reply: Frame, *, keep: bool) -> Frame:
        """``reply``, once ``conn`` is kept for the next request or closed."""
        if keep:
            self._keep(conn)
        else:
            conn.close()
        if not reply.get("ok"):
            code, error = reply.get("code"), reply.get("error")
            raise ServiceRefused(f"the embedding service refused ({code}): {error}", str(code))
        return reply

    def _connect(self, found: Discovery, key: bytes, paths: ServicePaths, deadline: float,
                 where: tuple[str, str, int]) -> _Conn:
        """A new connection to the service in ``found``, authenticated both ways."""
        import secrets
        import socket

        # A live service accepts on loopback at once. Nobody listening is refused
        # on POSIX, but on Windows only after about 2 s of retries: a connect that
        # takes this long is as good as refused (_recover then checks the lock).
        connect_s = max(0.001, min(CONNECT_S, deadline - time.monotonic()))
        try:
            sock = socket.create_connection(("127.0.0.1", found.port), timeout=connect_s)
        except TimeoutError as exc:
            raise _Unreachable(f"no answer to connect in {connect_s:.1f}s") from exc
        except OSError as exc:  # refused: nothing listens there now
            raise _Unreachable(str(exc)) from exc
        authenticated = False
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            nonce_c = secrets.token_hex(16)
            send_frame(sock, {"op": "hello", "proto": self.protocol, "nonce": nonce_c,
                              "client": self.version}, deadline)
            hello = recv_frame(sock, deadline, PREAUTH_MAX)
            if hello.get("proto") != self.protocol:
                raise EmbeddingUnavailable(
                    f"the running embedding service speaks protocol {hello.get('proto')!r} "
                    f"and this client {self.protocol}; it is left running")
            nonce_s, instance = hello.get("nonce"), hello.get("instance")
            if not (is_nonce(nonce_s) and isinstance(instance, str) and proofs_match(
                    hello.get("proof"), server_proof(key, nonce_c, str(nonce_s), instance))):
                # Not the service: something else holds its port, perhaps after it
                # died. Start one (throttled; a live service keeps its lock).
                if self.spawn:
                    self.start_service(paths)
                raise EmbeddingUnavailable(
                    f"the endpoint on port {found.port} failed authentication, so it isn't "
                    "this user's embedding service; nothing was sent to it")
            assert isinstance(nonce_s, str)
            send_frame(sock, {"op": "auth", "proof": client_proof(key, nonce_c, nonce_s)},
                       deadline)
            authenticated = True
            return _Conn(sock, where, _keepalive(hello))
        except TimeoutError as exc:
            raise EmbeddingUnavailable("the embedding service didn't answer in time") from exc
        except PeerGone as exc:
            raise _Gone(str(exc)) from exc
        except ProtocolError as exc:
            raise EmbeddingUnavailable(f"a bad frame from the embedding service: {exc}") \
                from exc
        except OSError as exc:  # reset or aborted mid-exchange
            raise _Gone(str(exc)) from exc
        finally:
            if not authenticated:
                sock.close()

    def _exchange(self, conn: _Conn, request: Frame, deadline: float, *, kept: bool) -> Frame:
        """Send ``request`` on ``conn`` and read the reply; ``conn`` is closed if that fails.

        On a kept connection, a send that fails, or an end before the first
        byte of the reply, means the service had closed it: :class:`_Stale`.
        """
        if "budget_ms" in request:
            # What is left now, after any connect and handshake: a budget worked
            # out before them let the service run work for a client that had
            # already given up.
            request = {**request, "budget_ms": max(
                1, int((deadline - time.monotonic()) * 1000))}
        sent = answered = False
        try:
            send_frame(conn.sock, request, deadline)
            sent = True
            reply = recv_frame(conn.sock, deadline, MAX_FRAME)
            answered = True
            return reply
        except TimeoutError as exc:
            raise EmbeddingUnavailable("the embedding service didn't answer in time") from exc
        except PeerGone as exc:
            if kept and not exc.started:
                raise _Stale(str(exc)) from exc
            raise _Gone(str(exc)) from exc
        except ProtocolError as exc:
            raise EmbeddingUnavailable(f"a bad frame from the embedding service: {exc}") \
                from exc
        except OSError as exc:  # reset or aborted mid-exchange
            if kept and not sent:
                raise _Stale(str(exc)) from exc
            raise _Gone(str(exc)) from exc
        finally:
            if not answered:
                conn.close()

    # ── Starting one ─────────────────────────────────────────────────

    def _recover(self, paths: ServicePaths, stale: Discovery) -> bool:
        """``stale`` says ready but took no connection: replace it if it is dead.

        A live service holds its root's lock for its whole life, and the kernel
        frees the lock when the process dies, zombie or not. So under the lock,
        a record still naming ``stale`` is a dead service's: it is cleared, and
        a replacement started at once, whoever started the last one. A held
        lock means the service is alive (busy, or draining): it is left alone.
        Returns True when a replacement was started.
        """
        if not self.spawn:
            return False
        from smriti.core.oslock import exclusive_lock

        cleared = False
        with exclusive_lock(paths.lock, wait_s=0) as held:
            if held:
                current = read_discovery(paths)
                if current is not None and current.instance == stale.instance:
                    clear_discovery(paths, stale.instance)
                    cleared = True
        if not cleared:
            return False
        log.warning("the embedding service %s (pid %d) is gone; starting another",
                    stale.instance, stale.pid)
        return self.start_service(paths, throttle=False)

    def start_service(self, paths: ServicePaths, *, throttle: bool = True) -> bool:
        """Start ``smriti embed serve`` for this root, unless one was started just now."""
        self._reap()
        try:
            age = time.time() - paths.spawn.stat().st_mtime
            if throttle and age < SPAWN_BACKOFF_S:
                return False
        except OSError:
            pass
        try:
            paths.spawn.write_text(str(os.getpid()), encoding="ascii")
        except OSError as exc:
            log.warning("could not write %s: %s", paths.spawn, exc)
        child = spawn_service(paths)
        with self._children_lock:
            self._children.append(child)
        return True


def _hold_loop(ref: Callable[[], ServiceEmbedding | None], stop: threading.Event) -> None:
    """The hold thread: a hold at once (its answer gives the interval), then one every
    ``idle_s / 3``, until it is stopped or its client is gone.

    While the service is coming up, the next hold comes sooner (from
    ``HOLD_RETRY_S``, doubling to ``HOLD_RETRY_MAX_S``, never past the
    interval): a replacement may idle out sooner than the last interval, and
    only a hold it answers says how soon.
    """
    wait, retry = 0.0, HOLD_RETRY_S
    while not stop.wait(wait):
        client = ref()
        if client is None:
            return
        try:
            outcome = client.hold_once()
        except Exception as exc:  # noqa: BLE001 — a hold is best effort; logged, the next one tries
            log.warning("holding the embedding service failed: %s: %s", type(exc).__name__, exc)
            outcome = "unavailable"
        if outcome.startswith(_HOLD_COMING_UP):
            wait, retry = min(client._hold_every, retry), min(retry * 2, HOLD_RETRY_MAX_S)
        else:
            wait, retry = client._hold_every, HOLD_RETRY_S
        del client  # not kept between holds: the reference is weak


def spawn_service(paths: ServicePaths) -> subprocess.Popen[bytes]:
    """Start ``python -m smriti embed serve --root <root>`` with a hidden console."""
    import subprocess

    # A hidden console, never DETACHED_PROCESS: a process with no console makes
    # every console child it runs open a visible window.
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    sink: IO[bytes] | None
    try:
        if paths.log.exists() and paths.log.stat().st_size > 5 * 1024 * 1024:
            paths.log.unlink()
        sink = paths.log.open("ab")
    except OSError:
        sink = None
    env = dict(os.environ, SMRITI_RUNTIME_DIR=str(paths.discovery.parent))
    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "smriti", "embed", "serve", "--root", str(paths.root)],
                stdin=subprocess.DEVNULL, stdout=sink or subprocess.DEVNULL,
                stderr=subprocess.STDOUT, creationflags=flags, env=env)
        else:
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "smriti", "embed", "serve", "--root", str(paths.root)],
                stdin=subprocess.DEVNULL, stdout=sink or subprocess.DEVNULL,
                stderr=subprocess.STDOUT, start_new_session=True, env=env)
    finally:
        if sink is not None:
            sink.close()  # the child holds its own handle
    log.info("started the embedding service for %s (pid %d)", paths.root, proc.pid)
    return proc


# ── Status and stop, for `smriti embed`, `smriti status` and smriti_status ──


@dataclass(frozen=True)
class ServiceState:
    state: str  # "off", "not running", "starting", "ready", "draining", "failed", "unreachable"
    discovery: Discovery | None = None
    live: dict[str, Any] | None = None  # the service's own answer, when it gave one
    error: str = ""

    def describe(self) -> str:
        d = self.discovery
        if self.state == "off":
            return "off (SMRITI_EMBED_SERVICE=0); this process embeds in-process"
        if self.state == "not running":
            return "not running; the next search or write starts it"
        if self.state == "failed" and d is not None:
            ago = max(0, int(time.time() - d.updated))
            return f"failed to start {ago}s ago: {d.error}"
        if d is None:
            return self.state
        parts = [f"{self.state}, pid {d.pid}"]
        if d.port:
            parts.append(f"port {d.port}")
        if d.identity is not None:
            parts.append(d.identity.describe())
        if self.live:
            stats = self.live.get("stats") or {}
            parts.append(f"served {stats.get('served', 0)}, queued {self.live.get('queued', 0)},"
                         f" idle {self.live.get('idle_for_s', 0):.0f}s of "
                         f"{self.live.get('idle_s', 0):.0f}s")
        if self.error:
            parts.append(self.error)
        return "; ".join(parts)


def service_state(root: Path, *, timeout_s: float = 0.5) -> ServiceState:
    """The service for ``root``: its discovery record and, when it answers, its status."""
    if not service_enabled():
        return ServiceState("off")
    try:
        runtime = runtime_dir(create=False)
    except PermissionError as exc:
        return ServiceState("unreachable", error=str(exc))
    paths = service_paths(runtime, root)
    found = read_discovery(paths)
    if found is None:
        return ServiceState("not running")
    alive = pid_alive(found.pid)
    if found.state == "failed":
        return ServiceState("failed", found)
    if not alive:
        return ServiceState("not running", found)
    if found.state != "ready" or not found.port:
        return ServiceState(found.state, found)
    client = ServiceEmbedding(root, spawn=False, hold=False)
    try:
        key = load_key(runtime, create=False)
        live = client._call(found, key, paths, time.monotonic() + timeout_s, {"op": "status"})
    except (EmbeddingUnavailable, _Gone, OSError) as exc:
        return ServiceState("unreachable", found, error=str(exc))
    finally:
        client.close()
    return ServiceState(str(live.get("state") or found.state), found, live)


def stop_service(root: Path, *, wait_s: float = 60.0) -> str:
    """Ask the service for ``root`` to drain and exit; wait for it. Returns what happened.

    A service still loading its model ("starting") can't take a request yet,
    so the discovery record is read again throughout the wait, and the stop
    is sent as soon as the service is ready. A draining one is only waited for.
    """
    runtime = runtime_dir(create=False)
    paths = service_paths(runtime, root)
    first = read_discovery(paths)
    if first is None or not pid_alive(first.pid) or first.state == "failed":
        return "not running"
    pid, instance = first.pid, first.instance
    client = ServiceEmbedding(root, spawn=False, hold=False)
    sent = False
    trouble = ""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return f"stopped (pid {pid})"
        found = read_discovery(paths)
        if found is not None and found.pid == pid:
            instance = found.instance  # the same process, now past "starting"
            if found.state == "failed":
                return f"it failed to start: {found.error}"
            if not sent and found.state == "ready" and found.port:
                try:
                    client._call(found, load_key(runtime, create=False), paths,
                                 min(deadline, time.monotonic() + 5.0), {"op": "stop"})
                    sent = True
                except (EmbeddingUnavailable, _Gone, OSError) as exc:
                    trouble = f"; asking it to stop failed: {exc}"  # tried again next look
        time.sleep(0.05)
    state = "not yet ready, so not asked to stop" if not sent else "asked to stop"
    return f"still running after {wait_s:.0f}s (pid {pid}, instance {instance}, {state}{trouble})"


_providers: dict[str, ServiceEmbedding] = {}
_providers_lock = threading.Lock()


def service_provider(root: Path) -> ServiceEmbedding:
    """This process's client for ``root``'s service (one per root)."""
    key = os.path.normcase(str(root.resolve()))
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            provider = _providers[key] = ServiceEmbedding(root)
        return provider

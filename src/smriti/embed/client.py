"""The embedding service's client: an embedding provider that holds no model.

:class:`ServiceEmbedding` asks the memory root's service for vectors. When
none is running it starts ``python -m smriti embed serve`` (a hidden
console on Windows, never a detached process) and waits, bounded. Every
step works to one deadline, from connecting to the last byte of the
answer: 3 s for a query, 30 s for a write's batch. When the deadline
passes, or the service can't be trusted (it fails authentication, or
speaks another protocol), the call raises :class:`EmbeddingUnavailable`
with the reason, and the caller degrades: search by keyword only, a write
with its vectors pending. A connection that closes before its answer
(the service exiting as the request arrived) is retried once, fresh.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

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
FAILED_BACKOFF_S = 30.0  # after a failed start, report it this long before trying again
_BATCH_BYTES = MAX_FRAME - 64 * 1024  # room for the frame's other fields


def service_enabled() -> bool:
    """``SMRITI_EMBED_SERVICE=0`` (or false/no/off) turns the service off."""
    value = os.environ.get("SMRITI_EMBED_SERVICE", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


class _Gone(Exception):
    """The connection ended before an answer: worth one fresh try."""


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
                 protocol: int = PROTOCOL, version: str | None = None) -> None:
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

    def _paths(self) -> tuple[Path, ServicePaths]:
        runtime = runtime_dir()
        return runtime, service_paths(runtime, self.root)

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
        return Embedded(vectors, identity)

    # ── One request ──────────────────────────────────────────────────

    def _request_batch(self, batch: list[str], kind: str, deadline: float) -> Embedded:
        gone: _Gone | None = None
        for _attempt in range(2):
            runtime, paths = self._paths()
            key = load_key(runtime)
            found = self._ready_service(paths, deadline)
            try:
                reply = self._call(found, key, paths, deadline, {
                    "op": "embed", "kind": kind, "texts": batch,
                    "budget_ms": max(1, int((deadline - time.monotonic()) * 1000))})
            except _Gone as exc:
                gone = exc
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

    def _ready_service(self, paths: ServicePaths, deadline: float) -> Discovery:
        """The ready service's discovery record, starting one if needed, by ``deadline``."""
        spawned = False
        delay = 0.02
        while True:
            found = read_discovery(paths)
            alive = found is not None and pid_alive(found.pid)
            if found is not None and alive and found.state == "ready" and found.port:
                return found
            if (found is not None and found.state == "failed"
                    and time.time() - found.updated < FAILED_BACKOFF_S):
                raise EmbeddingUnavailable(f"the embedding service failed to start: {found.error}")
            starting = found is not None and alive and found.state == "starting"
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
        """Authenticate both ways with the service in ``found``; send ``request``; the reply."""
        connect_s = max(0.001, min(1.0, deadline - time.monotonic()))
        try:
            sock = socket.create_connection(("127.0.0.1", found.port), timeout=connect_s)
        except TimeoutError as exc:
            raise EmbeddingUnavailable("connecting to the embedding service timed out") from exc
        except OSError as exc:  # refused or reset: it is gone (or going)
            raise _Gone(str(exc)) from exc
        with sock:
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
                send_frame(sock, request, deadline)
                reply = recv_frame(sock, deadline, MAX_FRAME)
            except TimeoutError as exc:
                raise EmbeddingUnavailable("the embedding service didn't answer in time") from exc
            except PeerGone as exc:
                raise _Gone(str(exc)) from exc
            except ProtocolError as exc:
                raise EmbeddingUnavailable(f"a bad frame from the embedding service: {exc}") \
                    from exc
            except OSError as exc:  # reset or aborted mid-exchange
                raise _Gone(str(exc)) from exc
        if not reply.get("ok"):
            code, error = reply.get("code"), reply.get("error")
            raise EmbeddingUnavailable(f"the embedding service refused ({code}): {error}")
        return reply

    # ── Starting one ─────────────────────────────────────────────────

    def start_service(self, paths: ServicePaths) -> bool:
        """Start ``smriti embed serve`` for this root, unless one was started just now."""
        for child in list(self._children):
            if child.poll() is not None:
                self._children.remove(child)  # reaped
        try:
            age = time.time() - paths.spawn.stat().st_mtime
            if age < SPAWN_BACKOFF_S:
                return False
        except OSError:
            pass
        try:
            paths.spawn.write_text(str(os.getpid()), encoding="ascii")
        except OSError as exc:
            log.warning("could not write %s: %s", paths.spawn, exc)
        self._children.append(spawn_service(paths))
        return True


def spawn_service(paths: ServicePaths) -> subprocess.Popen[bytes]:
    """Start ``python -m smriti embed serve --root <root>`` with a hidden console."""
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
    client = ServiceEmbedding(root, spawn=False)
    try:
        key = load_key(runtime, create=False)
        live = client._call(found, key, paths, time.monotonic() + timeout_s, {"op": "status"})
    except (EmbeddingUnavailable, _Gone, OSError) as exc:
        return ServiceState("unreachable", found, error=str(exc))
    return ServiceState(str(live.get("state") or found.state), found, live)


def stop_service(root: Path, *, wait_s: float = 60.0) -> str:
    """Ask the service for ``root`` to drain and exit; wait for it. Returns what happened."""
    runtime = runtime_dir(create=False)
    paths = service_paths(runtime, root)
    found = read_discovery(paths)
    if found is None or not pid_alive(found.pid) or found.state == "failed":
        return "not running"
    if found.state == "ready" and found.port:
        try:
            ServiceEmbedding(root, spawn=False)._call(
                found, load_key(runtime, create=False), paths, time.monotonic() + 5.0,
                {"op": "stop"})
        except (EmbeddingUnavailable, _Gone, OSError) as exc:
            return f"could not ask it to stop: {exc}"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not pid_alive(found.pid):
            return f"stopped (pid {found.pid})"
        time.sleep(0.05)
    return f"still running after {wait_s:.0f}s (pid {found.pid})"


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

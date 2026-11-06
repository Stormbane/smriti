"""The embedding service's wire protocol: framing, bounds, and mutual authentication.

Loopback TCP, stdlib only. A frame is a 4-byte big-endian length and that
many bytes of UTF-8 JSON (an object); never pickle. Every read and write
works to a deadline, so a stalled or slow-dripping peer costs at most that
long.

A connection goes::

    client -> {"op": "hello", "proto": 1, "nonce": Nc, "client": version}
    server -> {"proto": 1, "instance": I, "nonce": Ns, "proof": S, "keepalive_s": K,
               "hold": true}
    client -> {"op": "auth", "proof": C}
    client -> {"op": "embed" | "status" | "hold" | "stop", ...}
    server -> {"ok": true, ...} | {"ok": false, "code": ..., "error": ...}
    (more requests and replies, one at a time, while the service keeps it)

``S`` is an HMAC over the client's nonce and ``C`` one over the server's,
under the per-user key, with different labels so neither can be replayed as
the other. The client checks ``S`` before it sends anything else, so a
process that bound the port after the service died can't serve it; the
server checks ``C`` before it reads a request, so nobody without the key
gets an answer. Frames before authentication are at most 4 KB.

**Keep-alive.** A service whose hello carries ``keepalive_s`` reads further
requests on the same authenticated connection until the client closes it,
no request starts within ``K`` seconds of the last reply, or the service
drains. A client reuses a connection only when its hello said so, only
while it has been idle for less than ``K`` minus a margin, and sends a
``stop`` on a fresh one. Without the field (an older service) a connection
carries one request, and a client that doesn't know the field closes after
one, so every pairing of versions works; the protocol number is unchanged.

**Hold.** ``{"op": "hold"}`` does no work: it resets the service's idle
timer, and the answer carries ``instance``, ``state`` and ``idle_s``. A
service that knows it says ``"hold": true`` in its hello; an older one
answers ``{"ok": false, "code": "op"}``, which a client takes as "this
instance can't be held". Again the protocol number is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import socket  # only its type: the frames work on any socket the caller made

PROTOCOL = 1
MAX_FRAME = 4 * 1024 * 1024  # bytes in one frame, either way
PREAUTH_MAX = 4096  # bytes in a frame before the peer has proved it holds the key
MAX_TEXTS = 64  # texts in one request; clients split larger batches

_NONCE = re.compile(r"[0-9a-f]{32,64}")

Frame = dict[str, Any]


class ProtocolError(ValueError):
    """A frame that is malformed, or larger than allowed."""


class PeerGone(ConnectionError):
    """The peer closed or reset the connection before a whole frame arrived.

    ``started`` is whether any of the frame had arrived. A kept connection
    that ends before the first byte of a reply was closed by the service
    before it read the request, so the request can go again on a fresh one.
    """

    def __init__(self, message: str = "connection closed", *, started: bool = False) -> None:
        super().__init__(message)
        self.started = started


def _settimeout(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline passed")
    sock.settimeout(remaining)


def _recv_exact(sock: socket.socket, n: int, deadline: float, *, started: bool = False) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        _settimeout(sock, deadline)  # each read gets only what is left of the deadline
        try:
            chunk = sock.recv(min(n - len(buf), 65536))
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            raise PeerGone(f"connection reset: {exc}", started=started or bool(buf)) from exc
        if not chunk:
            raise PeerGone("connection closed", started=started or bool(buf))
        buf += chunk
    return bytes(buf)


def send_frame(sock: socket.socket, frame: Frame, deadline: float) -> None:
    data = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(data)} bytes is over the {MAX_FRAME}-byte limit")
    _settimeout(sock, deadline)
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_frame(sock: socket.socket, deadline: float, limit: int = MAX_FRAME) -> Frame:
    (n,) = struct.unpack(">I", _recv_exact(sock, 4, deadline))
    if n > limit:
        raise ProtocolError(f"frame of {n} bytes is over the {limit}-byte limit")
    try:
        frame = json.loads(_recv_exact(sock, n, deadline, started=True).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("frame is not a JSON object")
    return frame


def is_nonce(value: object) -> bool:
    return isinstance(value, str) and bool(_NONCE.fullmatch(value))


def server_proof(key: bytes, client_nonce: str, server_nonce: str, instance: str) -> str:
    import hmac  # here, not at the top: a reader of the discovery file needs none of it

    msg = f"smriti-embed/server|{client_nonce}|{server_nonce}|{instance}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def client_proof(key: bytes, client_nonce: str, server_nonce: str) -> str:
    import hmac

    msg = f"smriti-embed/client|{server_nonce}|{client_nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def proofs_match(given: object, expected: str) -> bool:
    import hmac

    return isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode())


def encode_vectors(vectors: list[list[float]]) -> str:
    """Vectors as base64 float64: exact (JSON floats are too, but three times larger)."""
    import base64

    flat = [x for v in vectors for x in v]
    return base64.b64encode(struct.pack(f"<{len(flat)}d", *flat)).decode("ascii")


def decode_vectors(text: object, count: int, dim: int) -> list[list[float]]:
    if not isinstance(text, str):
        raise ProtocolError("vectors missing")
    import base64

    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ProtocolError(f"vectors not base64: {exc}") from exc
    if dim <= 0 or len(raw) != count * dim * 8:
        raise ProtocolError(f"{len(raw)} bytes of vectors for {count} x {dim}")
    flat = struct.unpack(f"<{count * dim}d", raw)
    return [list(flat[i * dim:(i + 1) * dim]) for i in range(count)]

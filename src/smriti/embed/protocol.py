"""The embedding service's wire protocol: framing, bounds, and mutual authentication.

Loopback TCP, stdlib only. A frame is a 4-byte big-endian length and that
many bytes of UTF-8 JSON (an object); never pickle. Every read and write
works to a deadline, so a stalled or slow-dripping peer costs at most that
long.

A connection goes::

    client -> {"op": "hello", "proto": 1, "nonce": Nc, "client": version}
    server -> {"proto": 1, "instance": I, "nonce": Ns, "proof": S}
    client -> {"op": "auth", "proof": C}
    client -> {"op": "embed" | "status" | "stop", ...}
    server -> {"ok": true, ...} | {"ok": false, "code": ..., "error": ...}

``S`` is an HMAC over the client's nonce and ``C`` one over the server's,
under the per-user key, with different labels so neither can be replayed as
the other. The client checks ``S`` before it sends anything else, so a
process that bound the port after the service died can't serve it; the
server checks ``C`` before it reads a request, so nobody without the key
gets an answer. Frames before authentication are at most 4 KB.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import socket
import struct
import time
from typing import Any

PROTOCOL = 1
MAX_FRAME = 4 * 1024 * 1024  # bytes in one frame, either way
PREAUTH_MAX = 4096  # bytes in a frame before the peer has proved it holds the key
MAX_TEXTS = 64  # texts in one request; clients split larger batches

_NONCE = re.compile(r"[0-9a-f]{32,64}")

Frame = dict[str, Any]


class ProtocolError(ValueError):
    """A frame that is malformed, or larger than allowed."""


class PeerGone(ConnectionError):
    """The peer closed the connection before a whole frame arrived."""


def _settimeout(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline passed")
    sock.settimeout(remaining)


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        _settimeout(sock, deadline)  # each read gets only what is left of the deadline
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:
            raise PeerGone("connection closed")
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
        frame = json.loads(_recv_exact(sock, n, deadline).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("frame is not a JSON object")
    return frame


def is_nonce(value: object) -> bool:
    return isinstance(value, str) and bool(_NONCE.fullmatch(value))


def server_proof(key: bytes, client_nonce: str, server_nonce: str, instance: str) -> str:
    msg = f"smriti-embed/server|{client_nonce}|{server_nonce}|{instance}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def client_proof(key: bytes, client_nonce: str, server_nonce: str) -> str:
    msg = f"smriti-embed/client|{server_nonce}|{client_nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def proofs_match(given: object, expected: str) -> bool:
    return isinstance(given, str) and hmac.compare_digest(given.encode(), expected.encode())


def encode_vectors(vectors: list[list[float]]) -> str:
    """Vectors as base64 float64: exact (JSON floats are too, but three times larger)."""
    flat = [x for v in vectors for x in v]
    return base64.b64encode(struct.pack(f"<{len(flat)}d", *flat)).decode("ascii")


def decode_vectors(text: object, count: int, dim: int) -> list[list[float]]:
    if not isinstance(text, str):
        raise ProtocolError("vectors missing")
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise ProtocolError(f"vectors not base64: {exc}") from exc
    if dim <= 0 or len(raw) != count * dim * 8:
        raise ProtocolError(f"{len(raw)} bytes of vectors for {count} x {dim}")
    flat = struct.unpack(f"<{count * dim}d", raw)
    return [list(flat[i * dim:(i + 1) * dim]) for i in range(count)]

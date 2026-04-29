"""qmd backend — HTTP fast path with subprocess fallback.

Default backend. Talks to qmd's long-lived HTTP daemon
(``qmd mcp --http --daemon``) when reachable for sub-100ms warm
queries; falls back to ``qmd query`` subprocess when the daemon
isn't running (~3-4s cold per fire).

The HTTP path uses qmd's plain REST endpoint at ``POST /query``
which bypasses MCP entirely — no session handshake, no SDK, no
``Accept: text/event-stream`` dance. Body schema documented at
``qmd src/mcp/server.ts:653-704``::

    {"searches": [{"type": "lex|vec|hyde", "query": "..."}],
     "limit": 10, "minScore": 0, "intent": "..."}

Returns ``{"results": [{"docid","file","title","score",
"context","snippet"}, ...]}``.

Known issues handled here:
    - Upstream issue #452: npm's ``qmd.cmd`` shim invokes ``/bin/sh``,
      which Python's subprocess can't resolve on Windows without Git
      Bash. Fixed by calling ``node <qmd.js>`` directly when both are
      findable; falls back to the shim otherwise.
    - Upstream issue #519: the qwen3 reranker crashes with a CUDA
      error on some Windows + NVIDIA setups. We pass ``--no-rerank``
      by default and only enable rerank when ``SMRITI_RECALL_RERANK``
      is set. Once #519 lands upstream, the env-var default can flip.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from smriti.recall.types import RecallMatch, RecallResponse


def _resolve_qmd_cmd() -> list[str] | None:
    """Return argv prefix for invoking qmd, or None if it can't be found.

    Prefer ``node <qmd.js>`` directly to bypass the broken Windows shim.
    """
    home = Path.home()
    for candidate in (
        home / "AppData" / "Roaming" / "npm" / "node_modules"
            / "@tobilu" / "qmd" / "dist" / "cli" / "qmd.js",
        home / ".npm-global" / "lib" / "node_modules"
            / "@tobilu" / "qmd" / "dist" / "cli" / "qmd.js",
    ):
        if candidate.exists():
            node = shutil.which("node")
            if node:
                return [node, str(candidate)]
            break
    qmd = shutil.which("qmd")
    if qmd:
        return [qmd]
    return None


def is_available() -> bool:
    return _resolve_qmd_cmd() is not None


def _qmd_url() -> str:
    return os.environ.get("SMRITI_RECALL_QMD_URL", "http://localhost:8181").rstrip("/")


def daemon_health(*, timeout_s: float = 1.0) -> bool:
    """Quick liveness check on qmd's HTTP daemon."""
    try:
        with urllib.request.urlopen(
            f"{_qmd_url()}/health", timeout=timeout_s,
        ) as r:
            if r.status != 200:
                return False
            json.loads(r.read())
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError):
        return False


def _query_via_http(text: str, *, top_k: int, timeout_s: float) -> list[dict] | None:
    """Query qmd's HTTP daemon. Returns parsed results, or None on failure
    so the caller can fall through to the subprocess path."""
    body = json.dumps({
        "searches": [
            {"type": "lex", "query": text},
            {"type": "vec", "query": text},
        ],
        "limit": top_k,
    }).encode()
    req = urllib.request.Request(
        f"{_qmd_url()}/query",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, OSError):
        return None
    results = data.get("results")
    return results if isinstance(results, list) else None


def _query_via_subprocess(
    text: str, *, top_k: int, timeout_s: float, rerank: bool,
) -> tuple[list[dict], int | None, str | None]:
    """Cold-start fallback. Returns (results, returncode, stderr_tail)."""
    cmd = _resolve_qmd_cmd()
    if not cmd:
        return [], None, "qmd_not_found"

    args = [*cmd, "query", text, "-n", str(top_k), "--json"]
    if not rerank:
        args.append("--no-rerank")

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return [], None, "qmd_timeout"
    except Exception as exc:
        return [], None, f"qmd_run_failed: {str(exc)[:160]}"

    stdout = proc.stdout or ""
    start = stdout.find("[")
    end = stdout.rfind("]")
    parsed: list = []
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stdout[start:end + 1])
        except json.JSONDecodeError:
            parsed = []
    if not isinstance(parsed, list):
        parsed = []

    err = None
    if proc.returncode != 0 and not parsed:
        err = f"qmd_exit_{proc.returncode}: {(proc.stderr or '')[-160:]}"
    return parsed, proc.returncode, err


def _to_matches(items: list) -> list[RecallMatch]:
    out: list[RecallMatch] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(RecallMatch(
            source=str(item.get("file", "")),
            snippet=str(item.get("snippet", "")),
            score=float(item.get("score", 0.0)),
        ))
    return out


def query(text: str, *, top_k: int, timeout_s: float, rerank: bool) -> RecallResponse:
    t0 = time.monotonic()

    no_http = os.environ.get("SMRITI_RECALL_NO_HTTP", "").strip() == "1"
    if not no_http:
        # HTTP fast path. Use a tight timeout so a hung daemon doesn't
        # stall the parent tool — fall back to subprocess on any miss.
        http_results = _query_via_http(
            text, top_k=top_k, timeout_s=min(timeout_s, 5.0),
        )
        if http_results is not None:
            return RecallResponse(
                matches=_to_matches(http_results),
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                backend="qmd-http",
            )

    # Subprocess fallback (cold start, ~3-4s).
    items, _rc, err = _query_via_subprocess(
        text, top_k=top_k, timeout_s=timeout_s, rerank=rerank,
    )
    return RecallResponse(
        matches=_to_matches(items),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        backend="qmd-cli",
        error=err,
    )


def daemon_start() -> tuple[bool, str]:
    """Best-effort: start the qmd HTTP daemon in detached mode."""
    cmd = _resolve_qmd_cmd()
    if not cmd:
        return False, "qmd_not_found"
    if daemon_health(timeout_s=0.5):
        return True, "already_running"
    try:
        # On Windows, DETACHED_PROCESS keeps it alive after our process
        # exits. On POSIX, start_new_session does the same.
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [*cmd, "mcp", "--http", "--daemon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception as exc:
        return False, f"start_failed: {str(exc)[:160]}"

    # Poll until healthy or 10s elapses.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if daemon_health(timeout_s=0.5):
            return True, "started"
        time.sleep(0.5)
    return False, "start_timeout"


def daemon_stop() -> tuple[bool, str]:
    """Stop the qmd daemon via its own ``mcp stop`` command."""
    cmd = _resolve_qmd_cmd()
    if not cmd:
        return False, "qmd_not_found"
    try:
        proc = subprocess.run(
            [*cmd, "mcp", "stop"], capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return False, f"stop_failed: {str(exc)[:160]}"
    return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()[:200]

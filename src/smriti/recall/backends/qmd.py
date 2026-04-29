"""qmd backend — shells out to ``qmd query`` and parses JSON.

Default backend. Faster than smriti's embedded path because it avoids
loading torch/sentence-transformers per fire (qmd uses node-llama-cpp
+ GGUF, not Python).

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
import shutil
import subprocess
import time
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


def query(text: str, *, top_k: int, timeout_s: float, rerank: bool) -> RecallResponse:
    t0 = time.monotonic()
    cmd = _resolve_qmd_cmd()
    if not cmd:
        return RecallResponse(
            matches=[], elapsed_ms=0, backend="qmd", error="qmd_not_found",
        )

    args = [*cmd, "query", text, "-n", str(top_k), "--json"]
    if not rerank:
        args.append("--no-rerank")

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return RecallResponse(
            matches=[], elapsed_ms=int((time.monotonic() - t0) * 1000),
            backend="qmd", error="qmd_timeout",
        )
    except Exception as exc:
        return RecallResponse(
            matches=[], elapsed_ms=int((time.monotonic() - t0) * 1000),
            backend="qmd", error=f"qmd_run_failed: {str(exc)[:160]}",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # qmd writes progress to stderr and a JSON array to stdout. Be
    # defensive in case it ever interleaves anything else into stdout.
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

    matches: list[RecallMatch] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        matches.append(RecallMatch(
            source=str(item.get("file", "")),
            snippet=str(item.get("snippet", "")),
            score=float(item.get("score", 0.0)),
        ))

    err = None
    if proc.returncode != 0 and not matches:
        err = f"qmd_exit_{proc.returncode}: {(proc.stderr or '')[-160:]}"

    return RecallResponse(
        matches=matches, elapsed_ms=elapsed_ms, backend="qmd", error=err,
    )

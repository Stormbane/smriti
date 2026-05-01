"""Ollama provider — fully-local inference via the Ollama HTTP API.

For air-gapped runs or when sovereignty matters more than capability.
No external dependencies — uses stdlib ``urllib``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)


class OllamaProvider:
    name = "ollama"

    def __init__(self) -> None:
        self._base = os.environ.get(
            "SMRITI_OLLAMA_URL", "http://localhost:11434"
        ).rstrip("/")

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._base}/api/tags", timeout=1) as r:
                return r.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def default_model(self, role: str = "executor") -> str:
        if role == "judge":
            return os.environ.get("SMRITI_OLLAMA_JUDGE_MODEL", "llama3.2:3b")
        return os.environ.get("SMRITI_OLLAMA_EXECUTOR_MODEL", "llama3.1:8b")

    def call(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model()
        body: dict = {
            "model": model,
            "messages": [],
            "stream": False,
            "options": {"num_predict": request.max_tokens},
        }
        if request.system:
            body["messages"].append({"role": "system", "content": request.system})
        body["messages"].append({"role": "user", "content": request.user})
        if request.response_format == "json":
            body["format"] = "json"

        req = urllib.request.Request(
            f"{self._base}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = request.timeout_s or 600
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimitExceeded(f"ollama 429: {exc.reason}") from exc
            raise LLMError(f"ollama HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"ollama call failed: {exc}") from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        text = (data.get("message") or {}).get("content", "")
        return LLMResponse(
            text=text,
            model=model,
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            elapsed_ms=elapsed_ms,
        )

"""OpenAI API provider.

Auto-detected when OPENAI_API_KEY is set and the openai SDK imports
cleanly. Defaults to gpt-4o-mini for JUDGE, gpt-4o for EXECUTOR.
"""

from __future__ import annotations

import logging
import os
import time

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)


class OpenAIApiProvider:
    name = "openai_api"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import openai
        except ImportError:
            return None
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        base_url = os.environ.get("OPENAI_BASE_URL")  # optional override
        kwargs: dict = {"api_key": key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
        return self._client

    def is_available(self) -> bool:
        return self._get_client() is not None

    def default_model(self, role: str = "executor") -> str:
        if role == "judge":
            return os.environ.get("SMRITI_OPENAI_JUDGE_MODEL", "gpt-4o-mini")
        return os.environ.get("SMRITI_OPENAI_EXECUTOR_MODEL", "gpt-4o")

    def call(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        if client is None:
            raise LLMError(
                "openai SDK unavailable or OPENAI_API_KEY unset."
            )

        model = request.model or self.default_model()
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.user})

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            cls = exc.__class__.__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in cls or "rate limit" in msg or "quota" in msg:
                raise RateLimitExceeded(str(exc)[:300]) from exc
            raise LLMError(f"openai API call failed: {exc}") from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        usage = response.usage
        return LLMResponse(
            text=text,
            model=model,
            tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
            tokens_out=getattr(usage, "completion_tokens", 0) if usage else 0,
            elapsed_ms=elapsed_ms,
        )

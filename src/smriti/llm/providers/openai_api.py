"""OpenAI API provider.

Auto-detected when OPENAI_API_KEY is set and the openai SDK imports
cleanly. Defaults to gpt-4o-mini for JUDGE, gpt-4o for EXECUTOR.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from openai import OpenAI
    from openai.types.chat import ChatCompletionMessageParam
    from openai.types.chat.completion_create_params import CompletionCreateParamsNonStreaming

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)


class OpenAIApiProvider:
    name = "openai_api"

    def __init__(self) -> None:
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI | None:
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
        kwargs: dict[str, Any] = {"api_key": key}
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
        messages: list[ChatCompletionMessageParam] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for m in request.turns():
            # Both Message roles have this shape; mypy cannot select a TypedDict union.
            messages.append(cast("ChatCompletionMessageParam", {
                "role": m.role, "content": m.content,
            }))

        kwargs: CompletionCreateParamsNonStreaming = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        import openai  # imported with the client (_get_client)

        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(**kwargs)
        except (openai.OpenAIError, ValueError) as exc:
            # What a failed call raises: the SDK's errors (its transport's are
            # wrapped as APIConnectionError), and ValueError, for a reply body
            # that isn't JSON. Anything else is a bug, and is raised as it is.
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

"""Anthropic SDK provider with prompt caching.

System prompts are sent with ``cache_control: ephemeral`` so repeated
calls within the 5-minute TTL get a 90% discount on those input tokens.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from anthropic import Anthropic
    from anthropic.types import MessageParam

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)


class AnthropicApiProvider:
    name = "anthropic_api"

    def __init__(self) -> None:
        self._client: Anthropic | None = None

    def _get_client(self) -> Anthropic | None:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError:
            return None
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def is_available(self) -> bool:
        return self._get_client() is not None

    def default_model(self) -> str:
        # The variable's name is from when calls had roles (judge, executor);
        # an install that sets it keeps its model.
        return os.environ.get(
            "SMRITI_EXECUTOR_MODEL", "claude-sonnet-4-6-20250514"
        )

    def call(self, request: LLMRequest) -> LLMResponse:
        client = self._get_client()
        if client is None:
            raise LLMError(
                "anthropic SDK unavailable or ANTHROPIC_API_KEY unset. "
                "Either set the env var or switch SMRITI_LLM_PROVIDER."
            )

        import anthropic  # imported with the client (_get_client)

        t0 = time.monotonic()
        try:
            response = client.messages.create(
                model=request.model or self.default_model(),
                max_tokens=request.max_tokens,
                system=[{
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }],
                # Cast, not inferred: the SDK's MessageParam is one TypedDict in some
                # releases and a union of them in others, and a dict literal can't be
                # matched to a union member by mypy.
                messages=[
                    cast("MessageParam", {"role": m.role, "content": m.content})
                    for m in request.turns()
                ],
            )
        except (anthropic.AnthropicError, ValueError) as exc:
            # What a failed call raises: the SDK's errors (its transport's are
            # wrapped as APIConnectionError), and ValueError, for a reply body
            # that isn't JSON or a non-streaming call too long for the SDK's
            # timeout. Anything else is a bug, and is raised as it is. Rate
            # limits become RateLimitExceeded so a caller can stop the loop.
            cls = exc.__class__.__name__.lower()
            msg = str(exc).lower()
            if "ratelimit" in cls or "rate limit" in msg or "quota" in msg:
                raise RateLimitExceeded(str(exc)[:300]) from exc
            raise LLMError(f"anthropic API call failed: {exc}") from exc

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        usage = getattr(response, "usage", None)
        text = ""
        for block in getattr(response, "content", []) or []:
            if hasattr(block, "text"):
                text += block.text

        out = LLMResponse(
            text=text,
            model=request.model or self.default_model(),
            tokens_in=getattr(usage, "input_tokens", 0) if usage else 0,
            tokens_out=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_read_tokens=(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            ) if usage else 0,
            cache_creation_tokens=(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            ) if usage else 0,
            elapsed_ms=elapsed_ms,
        )
        out.cost_usd = _estimate_cost(out.model, out)
        return out


# USD per million tokens: (input, output, cache read). Update if rates move.
_RATES = {
    "claude-haiku-4-5-20251001": (0.80, 4.0, 0.08),
    "claude-sonnet-4-6-20250514": (3.0, 15.0, 0.30),
    "claude-opus-4-6-20250514": (15.0, 75.0, 1.50),
}


def _estimate_cost(model: str, meta: LLMResponse) -> float:
    in_rate, out_rate, cache_rate = _RATES.get(model, _RATES["claude-sonnet-4-6-20250514"])
    fresh_in = meta.tokens_in - meta.cache_read_tokens
    cost = (
        (fresh_in / 1_000_000) * in_rate
        + (meta.cache_read_tokens / 1_000_000) * cache_rate
        + (meta.tokens_out / 1_000_000) * out_rate
    )
    return round(cost, 6)

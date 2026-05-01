"""Anthropic SDK provider with prompt caching.

System prompts are sent with ``cache_control: ephemeral`` so repeated
calls within the 5-minute TTL get a 90% discount on those input tokens.
"""

from __future__ import annotations

import logging
import os
import time

from smriti.llm.types import LLMError, LLMRequest, LLMResponse, RateLimitExceeded

log = logging.getLogger(__name__)


class AnthropicApiProvider:
    name = "anthropic_api"

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
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

    def default_model(self, role: str = "executor") -> str:
        # SMRITI_MODEL / SMRITI_EXECUTOR_MODEL preserved for back-compat.
        if role == "judge":
            return os.environ.get("SMRITI_MODEL", "claude-haiku-4-5-20251001")
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
                messages=[{"role": "user", "content": request.user}],
            )
        except Exception as exc:
            # Anthropic SDK raises typed errors; surface rate-limits as
            # RateLimitExceeded so the dispatcher can stop the loop.
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


# Same cost table as the legacy api_backend; keep in sync if rates move.
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

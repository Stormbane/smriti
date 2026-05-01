"""Backend-agnostic LLM types."""

from __future__ import annotations

from dataclasses import dataclass


class LLMError(RuntimeError):
    """Generic provider error (non-rate-limit failures)."""


class RateLimitExceeded(LLMError):
    """Raised when a provider reports the rate/quota limit is hit.

    Sleep dispatcher catches this to stop the cluster/cascade loop
    cleanly so we don't burn through every queued task with the same
    failure until the reset window.
    """


@dataclass
class LLMRequest:
    """A single LLM call.

    ``system`` is held separately so providers that support prompt
    caching (Anthropic SDK, OpenAI structured caching) can mark it
    cacheable. ``user`` carries the per-call content.
    """

    system: str
    user: str
    model: str | None = None
    max_tokens: int = 4096
    response_format: str = "text"  # "text" | "json"
    timeout_s: int | None = None


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0

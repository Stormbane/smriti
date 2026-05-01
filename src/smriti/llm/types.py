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
class Message:
    """One turn in a multi-turn conversation.

    ``role`` is "user" or "assistant" — system content rides on
    ``LLMRequest.system`` so prompt caching keeps working.
    """

    role: str
    content: str


@dataclass
class LLMRequest:
    """A single LLM call.

    ``system`` is held separately so providers that support prompt
    caching (Anthropic SDK, OpenAI structured caching) can mark it
    cacheable.

    For single-turn use, set ``user`` and leave ``messages`` None.
    For multi-turn, populate ``messages`` with the full history;
    ``user`` is ignored when ``messages`` is provided.
    """

    system: str
    user: str = ""
    messages: list[Message] | None = None
    model: str | None = None
    max_tokens: int = 4096
    response_format: str = "text"  # "text" | "json"
    timeout_s: int | None = None

    def turns(self) -> list[Message]:
        """Resolve to a concrete message list regardless of which
        field the caller used."""
        if self.messages is not None:
            return self.messages
        return [Message(role="user", content=self.user)]


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

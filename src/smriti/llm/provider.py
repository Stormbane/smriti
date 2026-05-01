"""LLMProvider protocol — what every backend implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from smriti.llm.types import LLMRequest, LLMResponse


@runtime_checkable
class LLMProvider(Protocol):
    """A pluggable LLM backend.

    Implementations live in ``smriti.llm.providers.<name>``. The factory
    instantiates and caches one per process. Providers should be cheap
    to construct; expensive resources (HTTP clients, model loading)
    should lazy-init on first ``call``.
    """

    name: str

    def is_available(self) -> bool:
        """Quick check (no network) — is this provider usable here?

        e.g. anthropic_api returns True iff the SDK imports and
        ``ANTHROPIC_API_KEY`` is set; claude_cli returns True iff the
        ``claude`` CLI is on PATH.
        """
        ...

    def default_model(self, role: str = "executor") -> str:
        """Provider-default model for the given role.

        ``role`` is "judge" or "executor". JUDGE wants fast/cheap
        (Haiku, GPT-4o-mini); EXECUTOR wants capable (Sonnet, GPT-4o).
        """
        ...

    def call(self, request: LLMRequest) -> LLMResponse:
        """Execute the request. Raise RateLimitExceeded on quota/rate
        limits; LLMError on other failures."""
        ...

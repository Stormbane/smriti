"""Provider selection + the high-level ``call_llm`` entry point."""

from __future__ import annotations

import logging
import os
from typing import Callable

from smriti.llm.provider import LLMProvider
from smriti.llm.types import LLMRequest, LLMResponse

log = logging.getLogger(__name__)


def _load_anthropic_api() -> LLMProvider:
    from smriti.llm.providers.anthropic_api import AnthropicApiProvider
    return AnthropicApiProvider()


def _load_claude_cli() -> LLMProvider:
    from smriti.llm.providers.claude_cli import ClaudeCliProvider
    return ClaudeCliProvider()


def _load_openai_api() -> LLMProvider:
    from smriti.llm.providers.openai_api import OpenAIApiProvider
    return OpenAIApiProvider()


def _load_ollama() -> LLMProvider:
    from smriti.llm.providers.ollama import OllamaProvider
    return OllamaProvider()


# Registry of provider name -> lazy loader. Add new providers here.
_REGISTRY: dict[str, Callable[[], LLMProvider]] = {
    "anthropic_api": _load_anthropic_api,
    "claude_api": _load_anthropic_api,  # alias
    "claude_cli": _load_claude_cli,
    "openai_api": _load_openai_api,
    "openai": _load_openai_api,         # alias
    "ollama": _load_ollama,
}

# Process-wide cache so we don't re-import providers per call.
_CACHE: dict[str, LLMProvider] = {}


def _autodetect() -> str:
    """Pick a provider when SMRITI_LLM_PROVIDER isn't set.

    Preference order favors capability and cost: API with caching beats
    CLI subscription beats local Ollama.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "anthropic_api"
        except ImportError:
            pass
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            return "openai_api"
        except ImportError:
            pass
    # claude_cli requires the Claude Code CLI on PATH; we let the
    # provider's is_available() decide at call time.
    return "claude_cli"


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured (or named) provider, instantiated once."""
    if name is None:
        name = os.environ.get("SMRITI_LLM_PROVIDER", "").strip().lower()
        if not name:
            name = _autodetect()
    name = name.lower()
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown LLM provider {name!r}. "
            f"Known: {sorted(set(_REGISTRY))}. "
            "Set SMRITI_LLM_PROVIDER to one of these or unset to auto-detect."
        )
    if name not in _CACHE:
        _CACHE[name] = _REGISTRY[name]()
    return _CACHE[name]


def call_llm(
    *,
    system: str,
    user: str,
    model: str | None = None,
    role: str = "executor",
    max_tokens: int = 4096,
    response_format: str = "text",
    timeout_s: int | None = None,
    provider: str | None = None,
) -> LLMResponse:
    """Run one LLM call against the configured provider.

    ``role`` is "judge" or "executor"; if ``model`` is None the
    provider's default for that role is used. Set ``provider`` to
    bypass the env var and force a specific backend (handy for tests).
    """
    p = get_provider(provider)
    if model is None:
        model = p.default_model(role)
    req = LLMRequest(
        system=system,
        user=user,
        model=model,
        max_tokens=max_tokens,
        response_format=response_format,
        timeout_s=timeout_s,
    )
    return p.call(req)

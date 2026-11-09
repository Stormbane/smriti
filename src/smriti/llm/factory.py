"""Provider selection + the high-level ``call_llm`` entry point."""

from __future__ import annotations

import logging
import os
from typing import Callable

from smriti.llm.provider import LLMProvider
from smriti.llm.types import LLMRequest, LLMResponse, Message

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


def _load_codex_cli() -> LLMProvider:
    from smriti.llm.providers.codex_cli import CodexCliProvider
    return CodexCliProvider()


# Registry of provider name -> lazy loader. Add new providers here.
_REGISTRY: dict[str, Callable[[], LLMProvider]] = {
    "anthropic_api": _load_anthropic_api,
    "claude_api": _load_anthropic_api,  # alias
    "claude_cli": _load_claude_cli,
    "openai_api": _load_openai_api,
    "openai": _load_openai_api,         # alias
    "ollama": _load_ollama,
    "codex_cli": _load_codex_cli,
    "codex": _load_codex_cli,           # alias
}


def _load_test_stub() -> LLMProvider:
    from smriti.llm.providers.stub import StubLLMProvider
    return StubLLMProvider()  # raises LLMError outside a test


# Selectable by name only, never auto-detected or listed: canned answers that
# refuse to run unless SMRITI_ALLOW_TEST_STUB=1 (smriti.llm.providers.stub).
_TEST_REGISTRY: dict[str, Callable[[], LLMProvider]] = {
    "test_stub": _load_test_stub,
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
    loader = _REGISTRY.get(name) or _TEST_REGISTRY.get(name)
    if loader is None:
        raise ValueError(
            f"Unknown LLM provider {name!r}. "
            f"Known: {sorted(set(_REGISTRY))}. "
            "Set SMRITI_LLM_PROVIDER to one of these or unset to auto-detect."
        )
    if name not in _CACHE:
        _CACHE[name] = loader()
    return _CACHE[name]


def list_providers() -> list[dict[str, object]]:
    """Enumerate registered providers with availability + default model.

    Each entry: ``{name, available, default_model, aliases}``.
    Aliases (e.g. ``claude_api`` for ``anthropic_api``) are folded into
    the canonical entry. Useful for ``/provider list``-style commands and
    for tooling that needs to introspect what's wired up.
    """
    # Group aliases under their canonical loader.
    canonical: dict[int, str] = {}  # id(loader) -> canonical name
    aliases: dict[str, list[str]] = {}
    for n, loader in _REGISTRY.items():
        key = id(loader)
        if key not in canonical:
            canonical[key] = n
            aliases[n] = []
        elif n != canonical[key]:
            aliases[canonical[key]].append(n)

    rows: list[dict[str, object]] = []
    for n in sorted(set(canonical.values())):
        try:
            p = get_provider(n)
            available = bool(p.is_available())
            model = p.default_model()
        except Exception as exc:  # noqa: BLE001 — a broken provider is reported in its row
            rows.append({
                "name": n, "available": False, "default_model": "",
                "aliases": aliases.get(n, []), "error": str(exc)[:200],
            })
            continue
        rows.append({
            "name": n,
            "available": available,
            "default_model": model,
            "aliases": aliases.get(n, []),
        })
    return rows


def call_llm(
    *,
    system: str,
    user: str = "",
    messages: list[Message] | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    response_format: str = "text",
    timeout_s: int | None = None,
    provider: str | None = None,
) -> LLMResponse:
    """Run one LLM call against the configured provider.

    Pass either ``user`` (single-turn) or ``messages`` (multi-turn). If
    ``model`` is None the provider's default model is used. Set
    ``provider`` to bypass the env var and force a specific backend
    (handy for tests).
    """
    if not user and messages is None:
        raise ValueError("call_llm: either 'user' or 'messages' must be set")
    p = get_provider(provider)
    if model is None:
        model = p.default_model()
    req = LLMRequest(
        system=system,
        user=user,
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        response_format=response_format,
        timeout_s=timeout_s,
    )
    return p.call(req)

"""Pluggable LLM provider interface — model and harness agnostic.

Smriti's cognitive cascade (JUDGE, EXECUTOR, summarize, consolidate)
runs on whatever LLM the user wires up. The default is the Claude
Code CLI subscription (no API key needed); set ``ANTHROPIC_API_KEY``
to use the Anthropic SDK directly with prompt caching; set
``SMRITI_LLM_PROVIDER=openai_api`` and ``OPENAI_API_KEY`` to run on
GPT, etc.

Public API:
    call_llm(system, user, *, model=None, ...) -> LLMResponse
    get_provider(name=None) -> LLMProvider
    LLMResponse, LLMProvider, LLMError, RateLimitExceeded

Selection rules (in ``factory.get_provider``):
    1. Explicit: SMRITI_LLM_PROVIDER env var.
    2. Auto: anthropic_api if ANTHROPIC_API_KEY is set and the SDK
       imports cleanly, else claude_cli (Claude Code subscription).

Each provider lives in ``smriti.llm.providers.<name>`` and conforms to
the LLMProvider protocol. To add a new one, drop a module there and
register it in ``factory._REGISTRY``.
"""

from __future__ import annotations

from smriti.llm.types import LLMRequest, LLMResponse, LLMError, RateLimitExceeded
from smriti.llm.provider import LLMProvider
from smriti.llm.factory import call_llm, get_provider

__all__ = [
    "LLMRequest", "LLMResponse", "LLMError", "RateLimitExceeded",
    "LLMProvider", "call_llm", "get_provider",
]

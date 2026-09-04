"""Tests for src/smriti/llm/.

The LLM provider abstraction is what makes smriti model-agnostic.
This file pins the contract layer (Message, LLMRequest.turns(),
factory routing, list_providers, autodetect rules) without invoking
any actual LLM. Provider-specific HTTP/CLI behavior is out of scope —
those need integration tests with VCR or live keys.
"""

from __future__ import annotations

import pytest

from smriti.llm import (
    LLMRequest,
    LLMResponse,
    Message,
    call_llm,
    get_provider,
    list_providers,
)
from smriti.llm import factory as llm_factory


# --- Message + LLMRequest.turns() ---------------------------------------

class TestMessageAndTurns:
    def test_request_with_user_only_resolves_to_one_user_turn(self):
        req = LLMRequest(system="S", user="hello")
        turns = req.turns()
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "hello"

    def test_request_with_messages_uses_them_directly(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="bye"),
        ]
        req = LLMRequest(system="S", messages=msgs)
        # turns() returns the same list (not a copy — that's intentional;
        # it's a read-only resolution).
        assert req.turns() is msgs

    def test_messages_field_takes_precedence_over_user(self):
        """When both are set, messages wins. (Caller decides which path
        they're on.)"""
        req = LLMRequest(
            system="S",
            user="should-be-ignored",
            messages=[Message("user", "actual")],
        )
        turns = req.turns()
        assert len(turns) == 1
        assert turns[0].content == "actual"

    def test_message_dataclass_round_trip(self):
        m = Message(role="assistant", content="response text")
        assert m.role == "assistant"
        assert m.content == "response text"


# --- factory.get_provider -----------------------------------------------

class TestGetProvider:
    def test_named_provider_returns_instance(self):
        p = get_provider("ollama")
        assert p.name == "ollama"

    def test_alias_resolves_to_same_provider_class(self):
        """Aliases share a loader, so they resolve to the same class.
        (Cache is keyed by the alias string, so they're separate
        instances — that's a known minor inefficiency, not a bug.)"""
        a = get_provider("openai")
        b = get_provider("openai_api")
        assert type(a) is type(b)
        assert a.name == b.name

    def test_claude_api_alias(self):
        a = get_provider("claude_api")
        b = get_provider("anthropic_api")
        assert type(a) is type(b)
        assert a.name == b.name

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_provider("totally-bogus")

    def test_explicit_env_overrides_autodetect(self, monkeypatch):
        # Clear cache so this test selects fresh from env.
        monkeypatch.setattr(llm_factory, "_CACHE", {})
        monkeypatch.setenv("SMRITI_LLM_PROVIDER", "ollama")
        # Even with API key set, env wins.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        p = get_provider()
        assert p.name == "ollama"

    def test_provider_caching(self):
        # Same name -> same instance across calls.
        p1 = get_provider("ollama")
        p2 = get_provider("ollama")
        assert p1 is p2


# --- factory._autodetect ------------------------------------------------

class TestAutodetect:
    def test_prefers_anthropic_api_when_key_set(self, monkeypatch):
        monkeypatch.delenv("SMRITI_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        # Stub the import so it succeeds without the SDK installed.
        import sys
        if "anthropic" in sys.modules:
            chosen = llm_factory._autodetect()
            assert chosen == "anthropic_api"

    def test_prefers_openai_when_only_openai_key(self, monkeypatch):
        monkeypatch.delenv("SMRITI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-y")
        import sys
        if "openai" in sys.modules:
            chosen = llm_factory._autodetect()
            assert chosen == "openai_api"

    def test_falls_back_to_claude_cli(self, monkeypatch):
        monkeypatch.delenv("SMRITI_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        chosen = llm_factory._autodetect()
        assert chosen == "claude_cli"


# --- factory.list_providers ---------------------------------------------

class TestListProviders:
    def test_lists_all_canonical_providers(self):
        rows = list_providers()
        names = {r["name"] for r in rows}
        assert names == {"anthropic_api", "claude_cli", "openai_api", "ollama", "codex_cli"}

    def test_aliases_grouped_under_canonical(self):
        rows = {r["name"]: r for r in list_providers()}
        assert "claude_api" in rows["anthropic_api"]["aliases"]
        assert "openai" in rows["openai_api"]["aliases"]
        # No-alias providers have empty list.
        assert rows["claude_cli"]["aliases"] == []
        assert rows["ollama"]["aliases"] == []

    def test_each_row_has_required_fields(self):
        for r in list_providers():
            assert "name" in r
            assert "available" in r
            assert "judge_model" in r
            assert "executor_model" in r
            assert "aliases" in r
            assert isinstance(r["available"], bool)


# --- factory.call_llm ---------------------------------------------------

class _StubProvider:
    """Captures the LLMRequest that comes through so tests can inspect it."""

    def __init__(self):
        self.name = "stub"
        self.last_request: LLMRequest | None = None

    def is_available(self) -> bool:
        return True

    def default_model(self, role: str = "executor") -> str:
        return f"stub-{role}-model"

    def call(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(text="stub response", model=request.model or "stub")


@pytest.fixture
def stub_provider(monkeypatch):
    stub = _StubProvider()
    monkeypatch.setattr(llm_factory, "_CACHE", {"stub": stub})
    monkeypatch.setattr(
        llm_factory, "_REGISTRY",
        {**llm_factory._REGISTRY, "stub": lambda: stub},
    )
    return stub


class TestCallLLM:
    def test_single_turn_via_user(self, stub_provider):
        resp = call_llm(system="S", user="hello", provider="stub")
        assert resp.text == "stub response"
        req = stub_provider.last_request
        assert req.user == "hello"
        assert req.messages is None

    def test_multi_turn_via_messages(self, stub_provider):
        msgs = [Message("user", "hi"), Message("assistant", "hey"), Message("user", "more")]
        resp = call_llm(system="S", messages=msgs, provider="stub")
        assert resp.text == "stub response"
        req = stub_provider.last_request
        assert req.messages is msgs

    def test_requires_user_or_messages(self, stub_provider):
        with pytest.raises(ValueError, match="user.*messages"):
            call_llm(system="S", provider="stub")

    def test_role_drives_default_model(self, stub_provider):
        call_llm(system="S", user="x", role="judge", provider="stub")
        assert stub_provider.last_request.model == "stub-judge-model"

        call_llm(system="S", user="x", role="executor", provider="stub")
        assert stub_provider.last_request.model == "stub-executor-model"

    def test_explicit_model_overrides_default(self, stub_provider):
        call_llm(system="S", user="x", model="custom-model", provider="stub")
        assert stub_provider.last_request.model == "custom-model"

    def test_max_tokens_threaded_through(self, stub_provider):
        call_llm(system="S", user="x", max_tokens=999, provider="stub")
        assert stub_provider.last_request.max_tokens == 999

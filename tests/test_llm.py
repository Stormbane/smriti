"""Tests for src/smriti/llm/.

The LLM provider abstraction is what makes smriti model-agnostic.
This file pins the contract layer (Message, LLMRequest.turns(),
factory routing, list_providers, autodetect rules) without invoking
any actual LLM. Provider-specific HTTP/CLI behavior is out of scope —
those need integration tests with VCR or live keys.
"""

from __future__ import annotations

import os
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


# --- claude_cli: the prompt ALWAYS rides stdin (2026-09-08 morning page) --

def test_claude_cli_pipes_prompt_via_stdin_never_argv(monkeypatch, tmp_path):
    """On Windows ``claude`` resolves to npm's claude.cmd shim and cmd.exe
    ends the command at the first newline: an argv prompt lost its body
    AND every flag after it (--allowedTools gone → WebSearch denied →
    the "I could not compose" apology paged Suti). Short prompts used
    to take the argv path; none may."""
    import json as _json
    import subprocess

    from smriti.llm.providers import claude_cli as mod
    from smriti.llm.types import LLMRequest

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input")
        if "--system-prompt-file" in cmd:
            path = cmd[cmd.index("--system-prompt-file") + 1]
            with open(path, encoding="utf-8") as f:
                seen["system"] = f.read()
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "claude.cmd")
    # The provider imports llm_workdir inside call(): patch the source.
    monkeypatch.setattr("smriti.llm.workdir.llm_workdir", lambda: tmp_path)

    provider = mod.ClaudeCliProvider()
    provider.call(LLMRequest(
        system="Sys line", user="short\nmulti-line\nprompt", max_tokens=10,
        cli_args=["--strict-mcp-config", "--allowedTools", "WebSearch"]))

    cmd = seen["cmd"]
    assert cmd[:4] == ["claude.cmd", "-p", "--output-format", "json"]
    assert not any("\n" in part for part in cmd), "prompt leaked into argv"
    i = cmd.index("--strict-mcp-config")
    assert cmd[i:i + 3] == ["--strict-mcp-config", "--allowedTools", "WebSearch"]
    assert seen["input"] == "short\nmulti-line\nprompt"


def test_claude_cli_isolates_internal_calls(monkeypatch, tmp_path):
    """Internal calls read only the workdir's settings (no user CLAUDE.md or
    hooks), and the system text is the real system prompt, given through a
    file that is removed after the call."""
    import json as _json
    import subprocess

    from smriti.llm.providers import claude_cli as mod
    from smriti.llm.types import LLMRequest

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        path = cmd[cmd.index("--system-prompt-file") + 1]
        with open(path, encoding="utf-8") as f:
            seen["system"], seen["path"] = f.read(), path
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "claude.cmd")
    monkeypatch.setattr("smriti.llm.workdir.llm_workdir", lambda: tmp_path)

    mod.ClaudeCliProvider().call(LLMRequest(system="Line one\nline two", user="hi"))

    cmd = seen["cmd"]
    s = cmd.index("--setting-sources")
    assert cmd[s + 1] == "project,local"
    assert cmd.count("--strict-mcp-config") == 1
    assert seen["system"] == "Line one\nline two"
    assert not os.path.exists(seen["path"]), "system prompt file left behind"


def test_llm_workdir_carries_the_isolation_settings(tmp_path, monkeypatch):
    import json as _json

    from smriti.llm import workdir

    monkeypatch.setattr(workdir, "tree_root", lambda: tmp_path)
    path = workdir.llm_workdir()
    settings = _json.loads((path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["claudeMdExcludes"] == ["**/CLAUDE.md"]
    assert settings["disableAllHooks"] is True
    assert settings["autoMemoryEnabled"] is False


def test_llm_workdir_initialises_safely_under_concurrency(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from smriti.llm import workdir

    monkeypatch.setattr(workdir, "tree_root", lambda: tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: workdir.llm_workdir(), range(32)))
    assert len(set(paths)) == 1
    settings = paths[0] / ".claude"
    assert [p.name for p in settings.iterdir()] == ["settings.json"]  # no temp files left


def test_the_morning_call_has_web_search_and_no_other_tool():
    """--allowedTools only permits WebSearch; --tools removes every other
    built-in tool, so a malicious search result can't reach local files
    (Codex, 2026-11-05)."""
    from smriti.daylog.morning import _COMPOSE_ARGS

    assert "--tools=WebSearch" in _COMPOSE_ARGS
    assert "--allowedTools=WebSearch" in _COMPOSE_ARGS
    assert "--strict-mcp-config" in _COMPOSE_ARGS


def test_the_digest_call_disables_every_tool_by_the_documented_flag():
    """`--tools=` is Claude Code's documented "no built-in tools";
    `--disallowedTools=*` relies on an undocumented wildcard, so it is only
    the second layer. tests/test_llm_tool_boundary.py runs the real CLI."""
    from smriti.daylog.digest import _NO_TOOLS_ARGS

    assert "--tools=" in _NO_TOOLS_ARGS and "--disallowedTools=*" in _NO_TOOLS_ARGS


def test_the_tool_flags_carry_their_values_attached():
    """No flag takes a separate value: a separate "" that a wrapper dropped
    would let the variadic --tools take the next flag as its value, and on
    Claude Code 2.1.283 `--tools --strict-mcp-config` loaded the account's
    MCP connectors. --strict-mcp-config comes first, where nothing precedes
    it to swallow it."""
    from smriti.daylog.digest import _NO_TOOLS_ARGS
    from smriti.daylog.morning import _COMPOSE_ARGS

    for args in (_NO_TOOLS_ARGS, _COMPOSE_ARGS):
        assert args[0] == "--strict-mcp-config"
        assert all(arg.startswith("--") for arg in args), args
        assert "--disable-slash-commands" in args


# --- The API providers' failures (H12.7) ---------------------------------
#
# Each catches what its SDK raises for a failed call (and ValueError, for a
# body that isn't JSON), not everything: a bug in the call is raised as it is.


class _Raises:
    """A client whose one call raises ``exc``: ``client.messages.create`` and
    ``client.chat.completions.create`` alike."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.messages = self.completions = self
        self.chat = self

    def create(self, **_kwargs: object) -> None:
        raise self.exc


def _sdk_errors(sdk):
    import httpx

    request = httpx.Request("POST", "https://api.example.invalid/v1")
    return {
        "connection": sdk.APIConnectionError(request=request),
        "status": sdk.InternalServerError(
            "server error", response=httpx.Response(500, request=request), body=None),
        "rate limit": sdk.RateLimitError(
            "rate limited", response=httpx.Response(429, request=request), body=None),
    }


@pytest.mark.parametrize("sdk_name,provider_name", [("anthropic", "anthropic_api"),
                                                   ("openai", "openai_api")])
def test_an_api_call_that_fails_is_an_llm_error(sdk_name, provider_name):
    from smriti.llm.types import LLMError, RateLimitExceeded

    sdk = pytest.importorskip(sdk_name)
    provider = get_provider(provider_name)
    errors = _sdk_errors(sdk)
    for kind in ("connection", "status"):
        provider._client = _Raises(errors[kind])
        with pytest.raises(LLMError) as caught:
            provider.call(LLMRequest(system="S", user="u"))
        assert not isinstance(caught.value, RateLimitExceeded)
        assert caught.value.__cause__ is errors[kind]
    provider._client = _Raises(errors["rate limit"])
    with pytest.raises(RateLimitExceeded):
        provider.call(LLMRequest(system="S", user="u"))
    import json

    provider._client = _Raises(json.JSONDecodeError("Expecting value", "<html>", 0))
    with pytest.raises(LLMError):
        provider.call(LLMRequest(system="S", user="u"))


@pytest.mark.parametrize("sdk_name,provider_name", [("anthropic", "anthropic_api"),
                                                   ("openai", "openai_api")])
def test_a_bug_in_an_api_call_is_not_taken_for_a_failed_call(sdk_name, provider_name):
    from smriti.llm.types import LLMError

    pytest.importorskip(sdk_name)
    provider = get_provider(provider_name)
    provider._client = _Raises(AttributeError("a bug"))
    with pytest.raises(AttributeError, match="a bug") as caught:
        provider.call(LLMRequest(system="S", user="u"))
    assert not isinstance(caught.value, LLMError)

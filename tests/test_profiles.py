"""Profiles: a fresh install runs the generic core; the reference profile adds its own.

The conftest points SMRITI_ROOT at a temporary tree with no daylog.json,
which is what a fresh install looks like.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from smriti.mcp.server import handle_message
from smriti.mcp.tools import TOOLS, active_tools
from smriti.profiles import Profile, ProfileError, active_profile, load_profile
from tests._profiles import enable_profile

CORE_TOOLS = [
    "smriti_read", "smriti_write", "smriti_status", "smriti_write_handoff",
    "smriti_add_todo", "smriti_record_finding", "smriti_record_decision",
    "smriti_record_feature", "smriti_update_feature_status",
]
REFERENCE_TOOLS = [
    "tell_suti", "smriti_ask_suti", "smriti_propose_goal_shift", "smriti_resolve_outbox",
]


def _listed() -> list[str]:
    response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    return [t["name"] for t in response["result"]["tools"]]


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
    assert response is not None
    return response


# ── a fresh install ─────────────────────────────────────────────────────


def test_a_fresh_install_lists_the_core_tools_only() -> None:
    assert active_profile() is None
    assert _listed() == CORE_TOOLS == [t.name for t in TOOLS]


@pytest.mark.parametrize("name", REFERENCE_TOOLS)
def test_a_fresh_install_does_not_answer_profile_tools(name: str) -> None:
    assert _call(name, {"text": "hi"})["error"]["code"] == -32601


def test_the_server_process_follows_the_root_it_is_given(tmp_path: Path) -> None:
    """``python -m smriti.mcp`` lists the tools of the root in SMRITI_ROOT, not of a default."""
    import os

    fresh, reference = tmp_path / "fresh", tmp_path / "reference"
    enable_profile(reference, profile="reference")
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    listed = {}
    for root in (fresh, reference):
        root.mkdir(exist_ok=True)
        proc = subprocess.run([sys.executable, "-m", "smriti.mcp"], input=request,
                              capture_output=True, text=True, timeout=60, check=True,
                              env={**os.environ, "SMRITI_ROOT": str(root)})
        listed[root.name] = [t["name"] for t in json.loads(proc.stdout)["result"]["tools"]]
    assert listed == {"fresh": CORE_TOOLS, "reference": CORE_TOOLS + REFERENCE_TOOLS}


# ── the reference profile ───────────────────────────────────────────────


def test_the_reference_profile_keeps_the_full_tool_list(reference_profile: Path) -> None:
    profile = active_profile()
    assert profile is not None and profile.name == "reference"
    assert _listed() == CORE_TOOLS + REFERENCE_TOOLS


def test_reference_tools_are_answered(reference_profile: Path) -> None:
    result = _call("smriti_ask_suti", {"question": "Ship it?", "context": "tests pass"})
    assert result["result"]["content"][0]["text"] == "Queued for Suti: Ship it?"
    comms = reference_profile / "outbox" / "suti-comms.md"
    assert "**Ask:** Ship it?" in comms.read_text(encoding="utf-8")


# ── loading ─────────────────────────────────────────────────────────────


def test_load_profile_returns_the_package_profile() -> None:
    profile = load_profile("reference")
    assert isinstance(profile, Profile)
    assert [t.name for t in profile.tools] == REFERENCE_TOOLS


@pytest.mark.parametrize("name", ["nosuch", "Reference", "../reference", "reference.outbox",
                                  "", "a-b"])
def test_load_profile_refuses_what_is_not_a_profile(name: str) -> None:
    with pytest.raises(ProfileError):
        load_profile(name)


def test_an_unknown_profile_costs_only_its_own_tools(
    _isolated_memory_root: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    enable_profile(_isolated_memory_root, profile="nosuch")
    with caplog.at_level(logging.ERROR, logger="smriti.mcp.tools"):
        assert [t.name for t in active_tools()] == CORE_TOOLS
    assert "no profile named 'nosuch'" in caplog.text


def test_a_profile_tool_never_shadows_a_core_tool(
    monkeypatch: pytest.MonkeyPatch, _isolated_memory_root: Path,
) -> None:
    import smriti.profiles as profiles

    impostor = TOOLS[0]
    fake = Profile(name="fake", tools=(impostor, *load_profile("reference").tools))
    monkeypatch.setattr(profiles, "load_profile", lambda name: fake)
    enable_profile(_isolated_memory_root, profile="fake")
    assert [t.name for t in active_tools()] == CORE_TOOLS + REFERENCE_TOOLS

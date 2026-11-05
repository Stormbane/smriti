"""Profiles: a fresh install runs the generic core; an installed profile adds its own.

The conftest points SMRITI_ROOT at a temporary tree with no daylog.json,
which is what a fresh install looks like. Profiles are found through
package metadata, so these tests install a synthetic one, ``sample``
(see ``tests/_profiles.py``).
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
from tests._profiles import SAMPLE_TOOL, enable_profile, install_profile, write_profile_package

CORE_TOOLS = [
    "smriti_read", "smriti_write", "smriti_status", "smriti_write_handoff",
    "smriti_add_todo", "smriti_record_finding", "smriti_record_decision",
    "smriti_record_feature", "smriti_update_feature_status",
]
# The reference profile's tools (profiles/reference/): a fresh install must not answer them.
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


def test_the_core_package_carries_no_reference_code() -> None:
    """The reference profile is its own package (profiles/reference/): none of it is in smriti."""
    import ast
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "smriti"
    assert (src / "profiles" / "__init__.py").is_file()
    assert not (src / "profiles" / "reference").exists()
    importers = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] == "smriti_reference" for n in names):
                importers.append(f"{path.relative_to(src)}:{node.lineno}")
        if re.search(r"""(import_module|__import__)\(\s*["']smriti_reference""", text):
            importers.append(str(path.relative_to(src)))
    assert not importers, f"the core imports the reference profile: {importers}"


def test_the_server_process_follows_the_root_it_is_given(tmp_path: Path) -> None:
    """``python -m smriti.mcp`` lists the tools of the root in SMRITI_ROOT, not of a default."""
    import os

    site = write_profile_package(tmp_path / "site")
    fresh, sample = tmp_path / "fresh", tmp_path / "sample"
    enable_profile(sample, profile="sample")
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    path = os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH", "")) if p)
    listed = {}
    for root in (fresh, sample):
        root.mkdir(exist_ok=True)
        proc = subprocess.run([sys.executable, "-m", "smriti.mcp"], input=request,
                              capture_output=True, text=True, timeout=60, check=True,
                              env={**os.environ, "SMRITI_ROOT": str(root), "PYTHONPATH": path})
        listed[root.name] = [t["name"] for t in json.loads(proc.stdout)["result"]["tools"]]
    assert listed == {"fresh": CORE_TOOLS, "sample": [*CORE_TOOLS, SAMPLE_TOOL]}


def test_without_a_profile_the_wake_names_the_configured_user(
    _isolated_memory_root: Path,
) -> None:
    from smriti.wake.briefing import briefing

    root = _isolated_memory_root
    assert "people/people.md  -- the people in your life" in briefing(root, cwd=root)
    enable_profile(root, user_name="Sam Lee")
    text = briefing(root, cwd=root)
    assert "people/sam-lee/sam-lee.md  -- Sam Lee, the partnership" in text
    assert "--- PROFILE" not in text


def test_an_unknown_profile_is_one_line_in_the_wake(_isolated_memory_root: Path) -> None:
    from smriti.wake.briefing import briefing

    enable_profile(_isolated_memory_root, profile="nosuch")
    text = briefing(_isolated_memory_root, cwd=_isolated_memory_root)
    assert "--- PROFILE 'nosuch': unavailable (ProfileError) ---" in text
    assert "--- READING LIST" in text


# ── the core tools name the configured parties ──────────────────────────


def _listing() -> dict[str, dict[str, Any]]:
    response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    return {t["name"]: t for t in response["result"]["tools"]}


def _owner_enum(listing: dict[str, dict[str, Any]]) -> list[str]:
    return listing["smriti_record_feature"]["inputSchema"]["properties"]["owner"]["enum"]


def test_core_tool_text_names_the_configured_parties(_isolated_memory_root: Path) -> None:
    generic = _listing()
    assert generic["smriti_read"]["description"].startswith("Search Agent's memory tree")
    assert _owner_enum(generic) == ["user", "agent", "both"]
    enable_profile(_isolated_memory_root, user_name="Sam", agent_name="Ari")
    named = _listing()
    assert named["smriti_read"]["description"].startswith("Search Ari's memory tree")
    assert "to the ari tree" in named["smriti_write"]["description"]
    assert "Sam still triggers /clear" in named["smriti_write_handoff"]["description"]
    assert _owner_enum(named) == ["sam", "ari", "both"]
    text = json.dumps(named)
    assert not [t for t in ("{user", "{agent") if t in text]
    assert "{project}" in text  # other braces are left alone


def test_the_reference_names_reproduce_the_old_tool_text(_isolated_memory_root: Path) -> None:
    # The names alone set this text; the reference profile's own tests check the rest.
    enable_profile(_isolated_memory_root, user_name="Suti", agent_name="Narada")
    listing = _listing()
    assert listing["smriti_read"]["description"].startswith("Search Narada's memory tree")
    assert "to the narada tree" in listing["smriti_write"]["description"]
    assert "Suti still triggers /clear" in listing["smriti_write_handoff"]["description"]
    assert _owner_enum(listing) == ["suti", "narada", "both"]


@pytest.mark.parametrize("config,owner,stored", [
    ({"user_name": "Sam", "agent_name": "Ari"}, "Sam", "sam"),
    ({"user_name": "Sam", "agent_name": "Ari"}, "suti", "both"),
    ({"user_name": "Suti", "agent_name": "Narada"}, "narada", "narada"),
    ({}, "agent", "agent"),
])
def test_a_feature_owner_is_one_of_the_configured_parties(
    _isolated_memory_root: Path, config: dict[str, str], owner: str, stored: str,
) -> None:
    enable_profile(_isolated_memory_root, **config)
    (_isolated_memory_root / "mirrors" / "demo" / "ai").mkdir(parents=True)
    result = _call("smriti_record_feature", {"project": "demo", "slug": "f", "title": "T",
                                             "goal": "g", "plan": "p", "owner": owner})
    assert not result["result"].get("isError"), result
    spec = _isolated_memory_root / "mirrors" / "demo" / "ai" / "features" / "active" / "f.md"
    assert f"owner: {stored}\n" in spec.read_text(encoding="utf-8")


# ── loading ─────────────────────────────────────────────────────────────


def test_an_installed_profile_is_loaded_through_its_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_memory_root: Path,
) -> None:
    install_profile(tmp_path / "site", monkeypatch)
    profile = load_profile("sample")
    assert isinstance(profile, Profile) and profile.name == "sample"
    assert [t.name for t in profile.tools] == [SAMPLE_TOOL]
    enable_profile(_isolated_memory_root, profile="sample", user_name="Sam")
    assert _listed() == [*CORE_TOOLS, SAMPLE_TOOL]
    assert _listing()[SAMPLE_TOOL]["description"] == "Wave to Sam."
    assert _call(SAMPLE_TOOL, {})["result"]["content"][0]["text"] == "Ari waves to Sam."


def test_an_installed_profile_adds_its_wake_sections_and_morning_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_memory_root: Path,
) -> None:
    from smriti.daylog.config import load_config
    from smriti.daylog.morning import _system_prompt
    from smriti.wake.briefing import briefing

    install_profile(tmp_path / "site", monkeypatch)
    enable_profile(_isolated_memory_root, profile="sample")
    text = briefing(_isolated_memory_root, cwd=_isolated_memory_root)
    assert "--- GARDEN ---\nThe beans are up.\n" in text
    assert _system_prompt(load_config(_isolated_memory_root)) == (
        "You are Ari, writing Sam one line to start the day.")


@pytest.mark.parametrize("name", ["nosuch", "Reference", "../reference", "reference.outbox",
                                  "", "a-b"])
def test_load_profile_refuses_what_is_not_a_profile(name: str) -> None:
    with pytest.raises(ProfileError):
        load_profile(name)


def test_a_profile_two_packages_provide_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_profile(tmp_path / "site", monkeypatch)
    install_profile(tmp_path / "site", monkeypatch, module="other_profile", dist="other-profile")
    with pytest.raises(ProfileError, match="'sample' is ambiguous") as raised:
        load_profile("sample")
    assert "other-profile 0" in str(raised.value) and "sample_profile 0" in str(raised.value)


@pytest.mark.parametrize("source,target,why", [
    ('raise RuntimeError("the beans are down")\n', "PROFILE",
     "RuntimeError: the beans are down"),
    ("import no_such_module_for_ari\n", "PROFILE", "ModuleNotFoundError"),
    ("PROFILE = 1 / 0\n", "PROFILE", "ZeroDivisionError"),
    ("PROFILE = None\n", "MISSING", "AttributeError"),
])
def test_a_profile_that_fails_to_load_costs_only_its_own_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_memory_root: Path,
    caplog: pytest.LogCaptureFixture, source: str, target: str, why: str,
) -> None:
    from smriti.wake.briefing import briefing

    install_profile(tmp_path / "site", monkeypatch, source=source, target=target)
    with pytest.raises(ProfileError, match="'sample'.*failed to load") as raised:
        load_profile("sample")
    assert why in str(raised.value) and raised.value.__cause__ is not None
    enable_profile(_isolated_memory_root, profile="sample")
    with caplog.at_level(logging.ERROR, logger="smriti.mcp.tools"):
        assert [t.name for t in active_tools()] == CORE_TOOLS
    assert "serving the core tools only" in caplog.text and why in caplog.text
    text = briefing(_isolated_memory_root, cwd=_isolated_memory_root)
    assert "--- PROFILE 'sample': unavailable (ProfileError) ---" in text
    assert "--- READING LIST" in text  # the rest of the wake is still there


def test_an_entry_point_that_names_no_profile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_profile(tmp_path / "site", monkeypatch, source='PROFILE = {"name": "sample"}\n')
    with pytest.raises(ProfileError, match="is dict, not a smriti.profiles.Profile"):
        load_profile("sample")


def test_an_unknown_profile_costs_only_its_own_tools(
    _isolated_memory_root: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    enable_profile(_isolated_memory_root, profile="nosuch")
    with caplog.at_level(logging.ERROR, logger="smriti.mcp.tools"):
        assert [t.name for t in active_tools()] == CORE_TOOLS
    assert "no installed package provides profile 'nosuch'" in caplog.text


_SHADOW_PROFILE = '''\
from smriti.mcp.tools import TOOLS
from smriti.profiles import Profile
from sample_profile import WAVE

PROFILE = Profile(name="shadow", tools=(TOOLS[0], WAVE))
'''


def test_a_profile_tool_never_shadows_a_core_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_memory_root: Path,
) -> None:
    install_profile(tmp_path / "site", monkeypatch)
    install_profile(tmp_path / "site", monkeypatch, name="shadow", module="shadow_profile",
                    source=_SHADOW_PROFILE)
    enable_profile(_isolated_memory_root, profile="shadow")
    assert [t.name for t in load_profile("shadow").tools] == [CORE_TOOLS[0], SAMPLE_TOOL]
    assert [t.name for t in active_tools()] == [*CORE_TOOLS, SAMPLE_TOOL]

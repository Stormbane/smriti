"""Profiles in tests: enable one in a memory root, or install one the way a package does.

A profile is found through installed package metadata (see
:mod:`smriti.profiles`), so the core's tests install a synthetic one,
``sample`` (the agent Ari, the user Sam), into a temporary site
directory rather than relying on any real profile package.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

SAMPLE_TOOL = "sample_wave"

# The synthetic profile: one tool, one wake section, a morning prompt.
SAMPLE_PROFILE = '''\
"""A synthetic profile installed by the tests: the agent Ari and the user Sam."""

from smriti.mcp.tool import Tool
from smriti.profiles import Profile, WakeSection


def _wave(arguments):
    return "Ari waves to Sam."


def _wake_sections(ctx):
    return [WakeSection("garden", "GARDEN", lambda: "--- GARDEN ---\\nThe beans are up.\\n")]


def _morning_prompt(cfg):
    return "You are Ari, writing Sam one line to start the day."


WAVE = Tool(name="sample_wave", description="Wave to {user}.",
            input_schema={"type": "object", "properties": {}}, handler=_wave)
PROFILE = Profile(name="sample", tools=(WAVE,), wake_sections=_wake_sections,
                  morning_prompt=_morning_prompt)
'''


def enable_profile(root: Path, **config: object) -> Path:
    """Merge *config* into ``<root>/.smriti/daylog.json``; returns the file."""
    path = root / ".smriti" / "daylog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    path.write_text(json.dumps({**current, **config}), encoding="utf-8")
    return path


def write_profile_package(directory: Path, *, name: str = "sample",
                          module: str = "sample_profile", dist: str | None = None,
                          source: str = SAMPLE_PROFILE, target: str = "PROFILE") -> Path:
    """Write *module* and the dist-info that registers it as profile *name*, as pip would.

    *directory* is then a site directory: with it on ``sys.path`` (or
    PYTHONPATH), ``importlib.metadata`` finds the entry point
    ``name = module:target`` in the ``smriti.profiles`` group, provided by
    the distribution *dist* (default: the module's name). Returns *directory*.
    """
    dist = dist or module
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{module}.py").write_text(source, encoding="utf-8")
    info = directory / f"{dist.replace('-', '_')}-0.dist-info"  # as a wheel names it
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text(f"[smriti.profiles]\n{name} = {module}:{target}\n",
                                           encoding="utf-8")
    return directory


def install_profile(directory: Path, monkeypatch: pytest.MonkeyPatch, *,
                    module: str = "sample_profile", **package: str) -> Path:
    """:func:`write_profile_package`, with *directory* on ``sys.path`` for this test only.

    The module is imported afresh from *directory* and forgotten when the
    test ends, so tests that install different code under one module name
    never see each other's.
    """
    write_profile_package(directory, module=module, **package)
    # Recorded as absent, so teardown removes whatever the test imports.
    monkeypatch.setitem(sys.modules, module, types.ModuleType(module))
    del sys.modules[module]
    monkeypatch.syspath_prepend(str(directory))
    importlib.invalidate_caches()
    return directory

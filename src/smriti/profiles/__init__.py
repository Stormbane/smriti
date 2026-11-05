"""Profiles: one installation's specifics, kept out of the generic core.

The core (capture, the nightly, the memory and project tools, the wake
briefing) knows two roles, a user and an agent, and the names config
gives them. What only makes sense for one particular installation, such
as tools that reach its user's phone, wake sections that read its health
app, or a persona for its morning message, lives in a profile: an
installed package that exposes a :class:`Profile` through the
``smriti.profiles`` entry-point group. Its ``pyproject.toml`` names it::

    [project.entry-points."smriti.profiles"]
    reference = "smriti_reference:PROFILE"

smriti itself ships no profile. The one in its repository,
``profiles/reference/`` (the distribution ``smriti-reference``), is the
maintainer's own installation; it is not a template.

A profile is enabled by config, ``"profile": "<name>"`` in
``<root>/.smriti/daylog.json``. With none set, the core runs alone: the
MCP server lists only its own tools, the wake briefing has no profile
sections, and the morning message uses the generic prompt.

A profile is code from the Python environment smriti runs in, found the
way any installed plugin is; nothing is ever loaded from the memory tree.
A profile that can't be loaded raises :class:`ProfileError`, so it costs
only what it adds: the MCP server serves the core's tools, and the wake
shows one line in place of its sections. The morning fails instead, since
a wrong persona must not go out quietly.

Every hook is optional:

- ``tools``: MCP tools, listed after the core's;
- ``wake_sections``: wake-briefing sections, placed after the system
  line and before the project files;
- ``morning_prompt``: the morning message's system prompt. The call can
  search the web only when ``cfg.morning_web_search`` is on, so the prompt
  should offer headlines only then.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smriti.daylog.config import DaylogConfig
    from smriti.mcp.tool import Tool

ENTRY_POINT_GROUP = "smriti.profiles"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProfileError(Exception):
    """The configured profile is not installed, is ambiguous, or can't be loaded."""


@dataclass(frozen=True)
class WakeContext:
    """What a profile's wake sections are built from."""

    memory_root: Path
    cwd: Path
    harness: str  # "claude" | "codex": whose MCP config applies
    now: datetime | None = None
    # Source overrides for the profile's own readers; tests pass a
    # profile-specific object here (via ``briefing(sources=...)``).
    sources: object | None = None


@dataclass(frozen=True)
class WakeSection:
    """One wake-briefing section a profile contributes.

    ``build`` returns the section's text, header line included. If it
    raises, the briefing shows ``--- <title> ---`` and a one-line note
    instead, so a broken source never breaks the wake. A ``pinned``
    section is never cut; any other is cut to ``floor`` after the
    journal and the project files, and before the identity summary.
    ``label`` is the truncation notice shown when it is cut.
    """

    name: str
    title: str
    build: Callable[[], str]
    cap: int | None = None
    floor: int = 0
    pinned: bool = False
    label: str = ""


@dataclass(frozen=True)
class Profile:
    """What one installation adds to the core. Every hook is optional."""

    name: str
    tools: tuple[Tool, ...] = ()
    wake_sections: Callable[[WakeContext], Sequence[WakeSection]] | None = None
    morning_prompt: Callable[[DaylogConfig], str] | None = None


def _provider(entry: EntryPoint) -> str:
    """The distribution an entry point comes from, for error messages."""
    dist = entry.dist
    return f"{dist.name} {dist.version}" if dist is not None else entry.value


@functools.lru_cache(maxsize=1)
def _installed() -> dict[str, tuple[EntryPoint, ...]]:
    """Every entry point in the group, by name: read once per process.

    Reading them opens every installed distribution's metadata (about
    150 ms in an environment of 700 packages), and the MCP server resolves
    its profile on each request.
    """
    found: dict[str, list[EntryPoint]] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        found.setdefault(entry.name, []).append(entry)
    return {name: tuple(entries) for name, entries in found.items()}


def _entries(name: str) -> tuple[EntryPoint, ...]:
    """The entry points called *name*; read again before answering none.

    A package installed after the first read (a server started before
    ``pip install``) is then found on the next call, not at a restart.
    """
    found = _installed().get(name, ())
    if not found:
        _installed.cache_clear()
        found = _installed().get(name, ())
    return found


def load_profile(name: str) -> Profile:
    """The installed profile called *name*.

    It is the object named by the one entry point called *name* in the
    ``smriti.profiles`` group. Raises :class:`ProfileError` when no
    installed package provides it, when more than one does, when loading
    it raises anything, and when what it names is not a :class:`Profile`.
    """
    if not _NAME_RE.match(name):
        raise ProfileError(f"profile name {name!r} is not a plain lower-case name")
    try:
        found = _entries(name)
    except (OSError, ValueError) as exc:  # an installed package's metadata is unreadable
        raise ProfileError(f"can't read the installed {ENTRY_POINT_GROUP!r} entry points: "
                           f"{exc}") from exc
    if not found:
        raise ProfileError(f"no installed package provides profile {name!r} "
                           f"(entry point group {ENTRY_POINT_GROUP!r})")
    if len(found) > 1:
        providers = ", ".join(sorted(_provider(entry) for entry in found))
        raise ProfileError(f"profile {name!r} is ambiguous: {len(found)} installed packages "
                           f"provide it ({providers})")
    entry = found[0]
    try:
        profile = entry.load()
    except Exception as exc:
        # Whatever the profile's own code raised: it costs only the profile.
        raise ProfileError(f"profile {name!r} ({entry.value}, from {_provider(entry)}) "
                           f"failed to load: {type(exc).__name__}: {exc}") from exc
    if not isinstance(profile, Profile):
        raise ProfileError(f"profile {name!r} ({entry.value}) is {type(profile).__name__}, "
                           "not a smriti.profiles.Profile")
    return profile


def active_profile(root: Path | None = None) -> Profile | None:
    """The profile ``daylog.json`` names for the tree at *root*, or None when it names none."""
    from smriti.daylog.config import load_config

    name = load_config(root).profile
    return load_profile(name) if name else None

"""Profiles: one installation's specifics, kept out of the generic core.

The core (capture, the nightly, the memory and project tools, the wake
briefing) knows two roles, a user and an agent, and the names config
gives them. What only makes sense for one particular installation, such
as tools that reach its user's phone, wake sections that read its health
app, or a persona for its morning message, lives in a profile: a
subpackage of ``smriti.profiles`` whose ``PROFILE`` is a :class:`Profile`.

A profile is enabled by config, ``"profile": "<name>"`` in
``<root>/.smriti/daylog.json``. With none set, the core runs alone: the
MCP server lists only its own tools, the wake briefing has no profile
sections, and the morning message uses the generic prompt.

Every hook is optional:

- ``tools``: MCP tools, listed after the core's;
- ``wake_sections``: wake-briefing sections, placed after the system
  line and before the project files;
- ``morning_prompt``: the morning message's system prompt.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smriti.daylog.config import DaylogConfig
    from smriti.mcp.tool import Tool

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ProfileError(Exception):
    """The configured profile does not exist or is not a profile."""


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


def load_profile(name: str) -> Profile:
    """The profile ``smriti.profiles.<name>``; raises :class:`ProfileError` if there is none."""
    if not _NAME_RE.match(name):
        raise ProfileError(f"profile name {name!r} is not a plain lower-case module name")
    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise  # the profile exists but something it imports does not
        raise ProfileError(f"no profile named {name!r} (smriti.profiles.{name})") from exc
    profile = getattr(module, "PROFILE", None)
    if not isinstance(profile, Profile):
        raise ProfileError(f"smriti.profiles.{name} has no PROFILE")
    return profile


def active_profile(root: Path | None = None) -> Profile | None:
    """The profile ``daylog.json`` names for the tree at *root*, or None when it names none."""
    from smriti.daylog.config import load_config

    name = load_config(root).profile
    return load_profile(name) if name else None

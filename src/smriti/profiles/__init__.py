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
way any installed plugin is, on the import path less the current directory
and the running script's: nothing is ever loaded from the memory tree (the
wake hook's script lives there) or from wherever a command was started.
Reading every installed package's metadata takes 150-200 ms in a large
environment, and every hook and MCP server is a new process, so what the
lookup found is cached in the runtime directory (:func:`_lookup`).
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
import importlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from smriti.daylog.config import DaylogConfig
    from smriti.mcp.tool import Tool

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "smriti.profiles"
CACHE_VERSION = 1
# A cache isn't written while a directory it is keyed on changed this recently:
# a second change within the same timestamp tick wouldn't show in its mtime.
RACY_S = 2.0
# What importlib.metadata.EntryPoint accepts as a value: module[:attr.path] [extras].
_VALUE_RE = re.compile(r"(?P<module>[\w.]+)\s*(:\s*(?P<attr>[\w.]+)\s*)?"
                       r"((?P<extras>\[.*\])\s*)?$")

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
    ``label`` is the truncation notice shown when it is cut. A ``memory``
    section shows recorded text (notes, captured conversations, another
    tool's records), so the briefing quotes it as data between memory tags,
    as it does the journal (see :mod:`smriti.wake.briefing`).
    """

    name: str
    title: str
    build: Callable[[], str]
    cap: int | None = None
    floor: int = 0
    pinned: bool = False
    label: str = ""
    memory: bool = False


@dataclass(frozen=True)
class Profile:
    """What one installation adds to the core. Every hook is optional."""

    name: str
    tools: tuple[Tool, ...] = ()
    wake_sections: Callable[[WakeContext], Sequence[WakeSection]] | None = None
    morning_prompt: Callable[[DaylogConfig], str] | None = None


@dataclass(frozen=True)
class _Entry:
    """One entry point in the group, and the distribution that provides it."""

    name: str
    value: str
    group: str
    dist: str = ""
    version: str = ""

    @property
    def provider(self) -> str:
        """The distribution, for error messages."""
        return f"{self.dist} {self.version}" if self.dist else self.value

    def load(self) -> object:
        """What the entry point names, imported as ``EntryPoint.load`` does.

        Not through ``importlib.metadata``, whose import alone (it brings the
        email package) costs more than the whole lookup from the cache.
        """
        match = _VALUE_RE.match(self.value)
        if match is None:
            raise ValueError(f"{self.value!r} is not a module[:attribute] reference")
        target: object = importlib.import_module(match.group("module"))
        for attr in filter(None, (match.group("attr") or "").split(".")):
            target = getattr(target, attr)
        return target


def _identity(path: str, st: os.stat_result) -> object:
    """What tells two path entries apart: the directory itself, however it is spelt."""
    return (st.st_dev, st.st_ino) if st.st_ino else os.path.normcase(path)


def _here() -> set[object]:
    """The current directory and the running script's: never searched."""
    places: list[str] = []
    try:
        places.append(os.getcwd())
    except OSError:
        pass  # a current directory that was removed
    main = sys.modules.get("__main__")
    script = getattr(main, "__file__", None)
    if getattr(main, "__spec__", None) is None and isinstance(script, str):
        # `python script.py` puts the script's directory first on the path;
        # with `-m` the first entry is the current directory, already here.
        places.append(os.path.dirname(os.path.abspath(script)))
    found: set[object] = set()
    for path in places:
        try:
            found.add(_identity(path, os.stat(path)))
        except OSError:
            pass  # gone: nothing on the path can be it
    return found


def _search_path() -> list[tuple[str, int]]:
    """Where a profile can come from: the import path, each entry with its mtime.

    Each ``sys.path`` entry that is an existing directory or file, once, in
    order, except ``''``, the current directory and the running script's
    directory. Those depend on where a process was started, not on what is
    installed; and the wake hook's script directory is the tree's
    ``.smriti``, which changes with every index write. Installing or
    removing a distribution changes its site directory's mtime (an editable
    install writes its dist-info there too), so the list keys the cache.
    Entries are told apart by what ``os.stat`` says they are, not by
    resolving each path, which costs more than the rest of the lookup.
    """
    skip = _here()
    found: list[tuple[str, int]] = []
    seen: set[object] = set()
    for entry in sys.path:
        if not isinstance(entry, str) or not entry:
            continue
        try:
            path = os.path.abspath(entry)
            st = os.stat(path)
        except (OSError, ValueError):
            continue  # not there (or not a path): nothing can be found on it
        ident = _identity(path, st)
        if ident in skip or ident in seen:
            continue
        seen.add(ident)
        found.append((path, st.st_mtime_ns))
    return found


def _normalized(dist: object) -> str:
    """A distribution's name as ``importlib.metadata`` compares them, cheaply when it can.

    From the dist-info directory's name where the standard library offers
    it (as ``entry_points()`` does), so a scan opens no package's METADATA
    except those that provide a profile.
    """
    name = getattr(dist, "_normalized_name", None)
    if isinstance(name, str) and name:
        return name
    raw = getattr(dist, "name", None)
    return re.sub(r"[-_.]+", "_", raw if isinstance(raw, str) else "").lower()


def _scan(paths: list[str]) -> list[_Entry]:
    """The group's entry points in the distributions on *paths*.

    As ``importlib.metadata.entry_points()`` reads them: the first
    distribution of each name, in path order.
    """
    from importlib.metadata import distributions

    entries: list[_Entry] = []
    seen: set[str] = set()
    for dist in distributions(path=paths):
        name = _normalized(dist)
        if name in seen:
            continue
        seen.add(name)
        text = dist.read_text("entry_points.txt")
        if not text or ENTRY_POINT_GROUP not in text:
            continue
        for ep in dist.entry_points.select(group=ENTRY_POINT_GROUP):
            entries.append(_Entry(ep.name, ep.value, ep.group, str(dist.name or ""),
                                  str(dist.version or "")))
    return entries


def _cache_file(paths: list[str]) -> Path:
    """The cache for this list of path entries, one file per list.

    Processes started differently (another interpreter, another
    PYTHONPATH) keep their own files, instead of replacing each other's.
    The name is a CRC, not a hash: the file holds the full fingerprint, so
    two lists that shared a name would only take turns, and hashlib's import
    alone costs more than the rest of the lookup.
    """
    import zlib

    from smriti.core.rundir import runtime_dir

    key = "\0".join(paths).encode("utf-8", "surrogatepass")
    return runtime_dir() / f"profiles-{zlib.crc32(key):08x}-{len(paths)}.json"


def _read_cache(file: Path, fingerprint: list[list[object]]) -> list[_Entry] | None:
    """The entries cached for *fingerprint*; None when the file is missing, stale or bad."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("version") != CACHE_VERSION
            or data.get("fingerprint") != fingerprint
            or not isinstance(data.get("entries"), list)):
        return None
    entries: list[_Entry] = []
    for item in data["entries"]:
        if not isinstance(item, dict):
            return None
        fields = [item.get(k) for k in ("name", "value", "group", "dist", "version")]
        strings = [f for f in fields if isinstance(f, str)]
        if len(strings) != 5 or strings[2] != ENTRY_POINT_GROUP:
            return None
        entries.append(_Entry(*strings))
    return entries


def _write_cache(file: Path, fingerprint: list[list[object]], entries: list[_Entry]) -> None:
    from smriti.core.files import write_atomic

    write_atomic(file, json.dumps({
        "version": CACHE_VERSION, "fingerprint": fingerprint,
        "entries": [{"name": e.name, "value": e.value, "group": e.group, "dist": e.dist,
                     "version": e.version} for e in entries]}, indent=1))


def _lookup(*, fresh: bool = False) -> list[_Entry]:
    """The group's entry points: from the cache when it matches the import path, else a scan.

    The cache lives in the private runtime directory, keyed on
    :func:`_search_path`. Unless *fresh*, a cache whose fingerprint (each
    entry and its mtime) matches is used as it is; a missing, unreadable or
    stale one means a scan, written back for the next process. The
    fingerprint is taken before the scan, so a change during the scan shows
    as stale next time. The cache never makes a lookup fail: without a
    usable runtime directory, every lookup scans.
    """
    found = _search_path()
    paths = [path for path, _ in found]
    fingerprint: list[list[object]] = [[path, mtime] for path, mtime in found]
    try:
        file: Path | None = _cache_file(paths)
    except OSError as exc:  # no usable runtime directory: no cache
        log.debug("profile lookup without a cache: %s", exc)
        file = None
    if file is not None and not fresh:
        cached = _read_cache(file, fingerprint)
        if cached is not None:
            return cached
    entries = _scan(paths)
    now = time.time_ns()
    if file is not None and all(abs(now - mtime) >= RACY_S * 1e9 for _, mtime in found):
        try:
            _write_cache(file, fingerprint, entries)
        except OSError as exc:
            log.debug("could not write the profile cache %s: %s", file, exc)
    return entries


@functools.lru_cache(maxsize=1)
def _installed(fresh: bool = False) -> dict[str, tuple[_Entry, ...]]:
    """Every entry point in the group, by name: looked up once per process.

    The MCP server resolves its profile on each request; :func:`_lookup`
    makes the first lookup in each new process cheap too.
    """
    found: dict[str, list[_Entry]] = {}
    for entry in _lookup(fresh=fresh):
        found.setdefault(entry.name, []).append(entry)
    return {name: tuple(entries) for name, entries in found.items()}


def _entries(name: str) -> tuple[_Entry, ...]:
    """The entry points called *name*; scanned afresh before answering none.

    A package installed after the first read (a server started before
    ``pip install``) is then found on the next call, not at a restart, and
    the scan refreshes the cache.
    """
    found = _installed().get(name, ())
    if not found:
        _installed.cache_clear()
        found = _installed(True).get(name, ())
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
        providers = ", ".join(sorted(entry.provider for entry in found))
        raise ProfileError(f"profile {name!r} is ambiguous: {len(found)} installed packages "
                           f"provide it ({providers})")
    entry = found[0]
    try:
        profile = entry.load()
    except Exception as exc:  # noqa: BLE001 — third-party code; raised again as a ProfileError
        # Whatever the profile's own code raised: it costs only the profile.
        raise ProfileError(f"profile {name!r} ({entry.value}, from {entry.provider}) "
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

"""Validation for tool arguments that become parts of file paths.

MCP tool arguments come from an agent, and what an agent sends can be steered
by content it has read (prompt injection). Any argument that names a project,
a feature slug or an outbox file becomes one segment of a path, so it must be
a plain name: letters, digits, ``.``, ``_`` and ``-``, starting with a letter
or digit. That rules out separators, ``..``, drive and home prefixes, and
control characters. ``smriti_write``'s branch, which may be nested, has its
own check (``smriti.store.writer._checked_branch``).
"""

from __future__ import annotations

import re

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Arguments that every MCP tool must pass through safe_name before use.
PATH_ARGUMENTS = ("project", "slug", "file")


class UnsafeName(ValueError):
    """An argument that would let a path leave its directory."""


def safe_name(value: object, what: str = "name") -> str:
    """Return ``value`` stripped if it is a plain single path segment, else raise."""
    if not isinstance(value, str):
        raise UnsafeName(f"{what} must be a string")
    name = value.strip()
    if not _PLAIN.fullmatch(name) or ".." in name:
        raise UnsafeName(
            f"{what} {value!r} must be a plain name: letters, digits, '.', '_' or '-', "
            "with no path separators or '..'"
        )
    return name

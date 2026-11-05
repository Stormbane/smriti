"""Validation for tool arguments that become parts of file paths.

MCP tool arguments come from an agent, and what an agent sends can be steered
by content it has read (prompt injection). Any argument that names a project,
a feature slug or an outbox file becomes one segment of a path, so it must be
a single name that can't leave its directory: no separators, no ``.`` or
``..``, no drive colon or other characters Windows reserves, no leading ``.``
or ``~``, no control characters, and not a Windows device name. Spaces and
non-ASCII letters are fine: the installer names project mirrors after their
folders verbatim. ``smriti_write``'s branch may be nested: its check
(``smriti.store.writer._checked_branch``) holds each of its segments to this
rule.
"""

from __future__ import annotations

# Arguments that every MCP tool must pass through safe_name before use.
PATH_ARGUMENTS = ("project", "slug", "file")

_RESERVED_CHARS = set('/\\:*?"<>|')
# Windows device names, including the superscript-digit aliases (COM¹ ...) it
# also treats as devices.
_PORT_DIGITS = [*"123456789", "¹", "²", "³"]
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"COM{d}" for d in _PORT_DIGITS), *(f"LPT{d}" for d in _PORT_DIGITS)}


class UnsafeName(ValueError):
    """An argument that would let a path leave its directory."""


def safe_name(value: object, what: str = "name") -> str:
    """Return ``value`` stripped if it is a safe single path segment, else raise."""
    if not isinstance(value, str):
        raise UnsafeName(f"{what} must be a string")
    name = value.strip()
    problem = None
    if not name or len(name) > 255:
        problem = "is empty or too long"
    elif name in (".", "..") or name.startswith((".", "~")) or name.endswith("."):
        problem = "starts with '.' or '~', or ends with '.'"
    elif any(c in _RESERVED_CHARS or ord(c) < 32 for c in name):
        problem = "contains a path separator, ':' or another reserved character"
    elif name.split(".")[0].rstrip().upper() in _DEVICE_NAMES:  # "CON .txt" is CON too
        problem = "is a reserved device name"
    if problem:
        raise UnsafeName(f"{what} {value!r} {problem}; it must be a single plain name")
    return name

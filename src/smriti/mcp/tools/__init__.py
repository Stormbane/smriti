"""The tools the server offers: the core's, then the active profile's.

``TOOLS`` is the core, memory and projects, the same for every install.
:func:`active_tools` adds the tools of the profile that
``<root>/.smriti/daylog.json`` names, if any (see :mod:`smriti.profiles`).
``scripts/doc_facts.py`` reads ``TOOLS`` here, and each profile's own
``TOOLS``, statically.
"""

from __future__ import annotations

import logging
from pathlib import Path

from smriti.mcp.tool import Tool
from smriti.mcp.tools import memory, projects

log = logging.getLogger(__name__)

TOOLS: tuple[Tool, ...] = (*memory.TOOLS, *projects.TOOLS)


def active_tools(root: Path | None = None) -> tuple[Tool, ...]:
    """Every tool for the install at *root*, in ``tools/list`` order: the core's, then the profile's.

    A profile that can't be loaded costs only its own tools: the core's
    are still served, and the reason goes to the server's log. A profile
    tool never replaces a core tool of the same name.
    """
    from smriti.profiles import ProfileError, active_profile

    try:
        profile = active_profile(root)
    except ProfileError as exc:
        log.error("serving the core tools only: %s", exc)
        return TOOLS
    if profile is None:
        return TOOLS
    core = {tool.name for tool in TOOLS}
    extra = tuple(tool for tool in profile.tools if tool.name not in core)
    for tool in profile.tools:
        if tool.name in core:
            log.error("profile %s: tool %s shadows a core tool; ignored", profile.name, tool.name)
    return (*TOOLS, *extra)

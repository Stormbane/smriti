"""The tools the server offers: the core's, then the active profile's.

``TOOLS`` is the core, memory and projects, the same for every install;
their text may name the user and the agent through the tokens of
:func:`smriti.mcp.tool.party_names`. :func:`active_tools` fills those in
from ``<root>/.smriti/daylog.json`` and adds the tools of the profile it
names, if any (see :mod:`smriti.profiles`). ``scripts/doc_facts.py``
reads ``TOOLS`` here, and each profile's own ``TOOLS``, statically.
"""

from __future__ import annotations

import logging
from pathlib import Path

from smriti.mcp.tool import Tool, party_names
from smriti.mcp.tools import memory, projects

log = logging.getLogger(__name__)

TOOLS: tuple[Tool, ...] = (*memory.TOOLS, *projects.TOOLS)


def active_tools(root: Path | None = None) -> tuple[Tool, ...]:
    """Every tool for the install at *root*, in ``tools/list`` order.

    The core's come first, then the profile's. A profile that can't be
    loaded costs only its own tools: the core's are still served, and the
    reason goes to the server's log. A profile tool never replaces a core
    tool of the same name.
    """
    from smriti.daylog.config import load_config
    from smriti.profiles import ProfileError, load_profile

    cfg = load_config(root)
    names = party_names(cfg.user_name, cfg.agent_name)
    core = tuple(tool.named(names) for tool in TOOLS)
    if not cfg.profile:
        return core
    try:
        profile = load_profile(cfg.profile)
    except ProfileError as exc:
        log.error("serving the core tools only: %s", exc)
        return core
    taken = {tool.name for tool in core}
    for tool in profile.tools:
        if tool.name in taken:
            log.error("profile %s: tool %s shadows a core tool; ignored", profile.name, tool.name)
    return (*core, *(tool.named(names) for tool in profile.tools if tool.name not in taken))

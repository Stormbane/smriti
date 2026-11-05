"""The reference profile: the maintainer's own installation.

smriti grew inside one setup, an agent named Narada working with one
user, Suti, and that setup still runs it every day. This profile holds
what only makes sense there:

- MCP tools that reach Suti: ``tell_suti`` (a message now, through the
  installation's fan-out sender), ``smriti_ask_suti`` and
  ``smriti_propose_goal_shift`` (queued in ``outbox/``), and
  ``smriti_resolve_outbox``. Their names and schemas are the ones the
  installation's prompts already use.
- Three wake-briefing sections (:mod:`.wake`, :mod:`.presence`): HANDS,
  SUTI NOW (the akhada health app and the latest ``people/`` notes) and
  ACROSS SURFACES (Telegram, the phone app, voice, notifications sent).

It is called "reference" because it is the installation the code is
developed and checked against, and its behaviour is the one the core must
keep when this profile is enabled. It is not a template or a recommended
setup: an adopter runs the core alone, or writes a profile of their own.

Enable it in ``<root>/.smriti/daylog.json``::

    {"profile": "reference", "user_name": "Suti", "agent_name": "Narada"}
"""

from __future__ import annotations

from collections.abc import Sequence

from smriti.mcp.tool import Tool
from smriti.profiles import Profile, WakeContext, WakeSection
from smriti.profiles.reference import outbox

# scripts/doc_facts.py reads this tuple statically to list the profile's tools.
TOOLS: tuple[Tool, ...] = (*outbox.TOOLS,)


def _wake_sections(ctx: WakeContext) -> Sequence[WakeSection]:
    from smriti.profiles.reference.wake import wake_sections  # only the wake needs it

    return wake_sections(ctx)


PROFILE = Profile(name="reference", tools=TOOLS, wake_sections=_wake_sections)

"""What a tool is, and how one refuses.

A tool is a name, a description and an input schema (what ``tools/list``
shows the agent) plus the handler that runs it. A handler returns the text
the agent reads. When it refuses — a missing argument, an unknown project —
it raises :class:`ToolError` with the reason; the server turns that, and any
other exception, into an ``isError`` result, so a failed call never reads
as a successful one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

Arguments = dict[str, Any]
Handler = Callable[[Arguments], str]


class ToolError(Exception):
    """A refusal the calling agent should read: say what was wrong and what to do."""


def party_names(user: str, agent: str) -> dict[str, str]:
    """The tokens a tool's text may carry, for the configured user and agent.

    ``{user}`` and ``{agent}`` are the display names (``daylog.json``'s
    ``user_name``, ``agent_name``); ``{user_key}`` and ``{agent_key}`` are
    their lower-case forms, for values such as an enum's.
    """
    return {"user": user, "agent": agent, "user_key": user.lower(), "agent_key": agent.lower()}


def _fill(value: Any, names: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for token, name in names.items():
            value = value.replace("{" + token + "}", name)
        return value
    if isinstance(value, dict):
        return {key: _fill(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, names) for item in value]
    return value


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def listing(self) -> dict[str, Any]:
        """The entry ``tools/list`` returns for this tool."""
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}

    def named(self, names: Mapping[str, str]) -> Tool:
        """This tool with the :func:`party_names` tokens in its text filled in.

        Only those exact tokens are replaced; other braces, such as a path's
        ``{project}``, are left alone.
        """
        return replace(self, description=_fill(self.description, names),
                       input_schema=_fill(self.input_schema, names))

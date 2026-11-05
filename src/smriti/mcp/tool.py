"""What a tool is, and how one refuses.

A tool is a name, a description and an input schema (what ``tools/list``
shows the agent) plus the handler that runs it. A handler returns the text
the agent reads. When it refuses — a missing argument, an unknown project —
it raises :class:`ToolError` with the reason; the server turns that, and any
other exception, into an ``isError`` result, so a failed call never reads
as a successful one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Arguments = dict[str, Any]
Handler = Callable[[Arguments], str]


class ToolError(Exception):
    """A refusal the calling agent should read: say what was wrong and what to do."""


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

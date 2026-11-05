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


# A JSON Schema scalar type, and the Python types its values arrive as.
_SCALARS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}
_ARTICLE = {"string": "a string", "integer": "an integer", "number": "a number",
            "boolean": "true or false"}


def check_arguments(schema: Mapping[str, Any], arguments: Arguments) -> None:
    """Hold ``arguments`` to the scalar types and bounds of ``schema``, in place.

    Each property the schema types as a string, integer, number or boolean
    must be one when it is given: a handler calls ``.strip()`` on a string,
    and a number in the wrong place failed deep inside it with an error that
    said nothing useful. A null is taken as not given, so the handler's
    default applies. An integer may arrive as a whole float (``5.0``) and is
    made an int. ``minimum`` and ``maximum`` bound integers and numbers.
    Arrays and objects are left to their handlers, which accept more than
    one shape (``tags`` as a list or a comma-separated string). Raises
    :class:`ToolError` naming the argument and what it must be.
    """
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return
    for key, spec in properties.items():
        if key not in arguments or not isinstance(spec, Mapping):
            continue
        value = arguments[key]
        if value is None:
            del arguments[key]
            continue
        kind = spec.get("type")
        if kind not in _SCALARS:
            continue
        if kind == "integer" and isinstance(value, float) and value.is_integer():
            value = arguments[key] = int(value)
        # bool is an int to Python, never to JSON.
        if isinstance(value, bool) != (kind == "boolean") or not isinstance(
                value, _SCALARS[kind]):
            raise ToolError(f"{key} must be {_ARTICLE[kind]}, not {_json_type(value)}")
        if kind not in ("integer", "number"):
            continue
        low, high = spec.get("minimum"), spec.get("maximum")
        if (low is not None and value < low) or (high is not None and value > high):
            bounds = (f"from {low} to {high}" if low is not None and high is not None
                      else f"at least {low}" if low is not None else f"at most {high}")
            raise ToolError(f"{key} must be {bounds} (got {value!r})")


def _json_type(value: object) -> str:
    """What ``value`` is, in JSON's words."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


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

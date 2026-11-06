"""Adapter for Codex CLI session rollouts.

Source: ``~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl``.

Codex has written the conversation in two encodings, and a file may hold
either, or both (a thread started in one and continued in the other):

- **legacy** (Codex CLI 0.145 and before, and threads still in the
  "legacy" history mode, such as the sessions Codex Desktop imports):
  ``event_msg`` records with payload type ``user_message`` or
  ``agent_message``, the text in ``payload.message``, the agent's phase
  in ``payload.phase``.
- **paginated** (the "paginated" history mode, from Codex CLI 0.153 in
  September 2026; 0.163 rewrote older rollouts into it on upgrade,
  compressed as ``.jsonl.zst``, which the source glob doesn't match):
  ``event_msg`` records with payload type ``item_completed`` whose
  ``payload.item`` is a ``UserMessage`` (text in its ``content`` blocks
  of type ``text``) or an ``AgentMessage`` (blocks of type ``Text``, the
  phase in ``item.phase``). It writes no ``user_message`` or
  ``agent_message`` record, which is why nothing was captured from it
  until H12.17.

Kept, in either: the user's messages (role ``user``) and the agent's
whose phase is ``final_answer`` or absent (role ``agent``).
``commentary``-phase agent messages are status narration during tool
work, not conversation, and are dropped, as is every other record:
``response_item`` (the model's copy of the conversation, with the
injected instructions, environment context, tool calls and their output),
reasoning, tool and file-change items, ``session_meta``,
``turn_context``, token counts.

A turn's id is the same in both encodings: the record's timestamp, the
role, the text and the session (from the file name). Codex's migration
kept each message's timestamp and text, so a message captured from the
legacy encoding has the id it would get from the paginated one (checked
on 40 rewritten rollouts whose turns the reference install had captured:
every id matched).
When one message is written in both encodings, a second copy within a
few seconds of the first is dropped.

Channel: ``codex:<project>`` from ``session_meta``'s ``cwd``, read
lazily from the file head (offset-based tailing may never revisit the
first line). A session that ran in smriti's own LLM working directory
(the nightly's ``codex exec`` fallback, see :mod:`smriti.llm.workdir`)
gives no turns: derived text must not feed back into the digests. Its
rollout's path doesn't say so, only its ``cwd`` does.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from smriti.daylog.adapters import ExtractResult, complete_lines, is_internal, project_of
from smriti.daylog.model import AGENT, USER, Turn, parse_ts

_UUID_RE = re.compile(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")

# The content blocks that carry a message's text, per item type.
_TEXT_BLOCKS = {"UserMessage": ("text",), "AgentMessage": ("Text", "text")}

# A message written in both encodings is one turn when the copies are this close.
_SAME_MESSAGE = timedelta(seconds=5)

_LEGACY = "legacy"
_PAGINATED = "paginated"


def _session_from_name(path: Path) -> str:
    m = _UUID_RE.search(path.name)
    return m.group(1) if m else path.stem[-8:]


class CodexRolloutAdapter:
    def __init__(self) -> None:
        self._head_cache: dict[str, tuple[str, bool]] = {}

    def _head(self, path: Path) -> tuple[str, bool]:
        """The file's channel, and whether its session ran in smriti's own LLM workdir."""
        key = str(path)
        cached = self._head_cache.get(key)
        if cached:
            return cached
        channel, internal = "codex", False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                head = f.readline()
            obj = json.loads(head)
            if isinstance(obj, dict) and obj.get("type") == "session_meta":
                cwd = str((obj.get("payload") or {}).get("cwd", "") or "")
                if cwd:
                    channel = f"codex:{project_of(cwd) or 'unknown'}"
                    internal = is_internal(cwd)
        except (OSError, json.JSONDecodeError):
            pass
        self._head_cache[key] = (channel, internal)
        return channel, internal

    def extract(self, path: Path, blob: bytes) -> ExtractResult:
        result = ExtractResult()
        lines, result.consumed = complete_lines(blob)
        if not lines:
            return result
        channel, internal = self._head(path)
        if internal:
            return result
        session = _session_from_name(path)
        # (role, text) -> (time, encoding) of the last turn kept, to drop a message's second copy
        kept: dict[tuple[str, str], tuple[datetime, str]] = {}
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                result.errors += 1
                continue
            if not isinstance(obj, dict) or obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                result.errors += 1
                continue
            message = _message(payload)
            if message is None:
                continue
            who, text, encoding = message
            ts = parse_ts(str(obj.get("timestamp", "")))
            if ts is None or not isinstance(text, str) or not text.strip():
                result.errors += 1
                continue
            text = text.strip()
            earlier = kept.get((who, text))
            if earlier and earlier[1] != encoding and abs(ts - earlier[0]) <= _SAME_MESSAGE:
                continue  # the same message in the other encoding
            kept[(who, text)] = (ts, encoding)
            result.turns.append(Turn(ts=ts, channel=channel, who=who, text=text, session=session))
        return result


def _message(payload: dict[str, object]) -> tuple[str, object, str] | None:
    """``(role, text, encoding)`` for a conversational message record; None for anything else."""
    ptype = payload.get("type")
    if ptype == "user_message":
        return USER, payload.get("message"), _LEGACY
    if ptype == "agent_message":
        if payload.get("phase") not in (None, "", "final_answer"):
            return None
        return AGENT, payload.get("message"), _LEGACY
    if ptype != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype == "UserMessage":
        who = USER
    elif itype == "AgentMessage":
        if item.get("phase") not in (None, "", "final_answer"):
            return None
        who = AGENT
    else:
        return None
    blocks = _TEXT_BLOCKS[str(itype)]
    content = item.get("content")
    parts = [block["text"] for block in (content if isinstance(content, list) else ())
             if isinstance(block, dict) and block.get("type") in blocks
             and isinstance(block.get("text"), str)]
    if not any(part.strip() for part in parts):
        return None  # no text (an image alone): nothing to say, and nothing malformed
    return who, "\n".join(parts), _PAGINATED

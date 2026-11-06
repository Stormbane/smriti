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
on the 118 rewritten rollouts of 28 sessions the reference install had
captured, read as capture reads them: every one of their 5,945 turns
outside smriti's llm-workdir came back with its id).
When one message is written in both encodings, a second copy within
five seconds of the first is dropped. What is kept mustn't depend on how
the file is split between reads (the two copies can fall in different
passes, or on either side of a read's end), so the adapter remembers the
messages it kept in the last five seconds of the file's time and hands
that back as the read's ``carry``, which capture keeps in the file's state
with its offset: each record is judged against the same memory whatever
came in the same read. The memory holds each message's role, a hash of
its text, its time and its encoding, never the text.

Channel: ``codex:<project>`` from ``session_meta``'s ``cwd``, read
lazily from the file head (offset-based tailing may never revisit the
first line). A session that ran in smriti's own LLM working directory
(the nightly's ``codex exec`` fallback, see :mod:`smriti.llm.workdir`)
gives no turns: derived text must not feed back into the digests. Its
rollout's path doesn't say so, only its ``cwd`` does.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from smriti.daylog.adapters import ExtractResult, complete_lines, is_internal, project_of
from smriti.daylog.model import AGENT, USER, Turn, parse_ts

_UUID_RE = re.compile(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")

# The content blocks that carry a message's text, per item type.
_TEXT_BLOCKS = {"UserMessage": ("text",), "AgentMessage": ("Text", "text")}

# A message written in both encodings is one turn when the copies are this close.
_SAME_MESSAGE_MS = 5_000
# The most kept messages remembered at once (a burst within five seconds).
_MAX_RECENT = 64
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)

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

    def extract(self, path: Path, blob: bytes,
                carry: list[list[Any]] | None = None) -> ExtractResult:
        result = ExtractResult()
        lines, result.consumed = complete_lines(blob)
        if not lines:
            return result
        channel, internal = self._head(path)
        if internal:
            return result
        session = _session_from_name(path)
        recent = _Recent(carry)
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
            if not recent.keep(who, text, ts, encoding):
                continue  # the same message in the other encoding
            result.turns.append(Turn(ts=ts, channel=channel, who=who, text=text, session=session))
        result.carry = recent.carry()
        return result


class _Recent:
    """The messages kept in the last five seconds of a file's time, to know a second copy.

    Each is ``[role, text hash, unix ms, encoding]``. It changes one record
    at a time, by the same rules within a read and across reads, so the
    copy kept is the same however the file is split.
    """

    def __init__(self, carry: list[list[Any]] | None) -> None:
        self._kept: dict[tuple[str, str], tuple[int, str]] = {}
        for entry in carry or ():
            if (len(entry) == 4 and isinstance(entry[0], str) and isinstance(entry[1], str)
                    and isinstance(entry[2], int) and isinstance(entry[3], str)):
                self._kept[(entry[0], entry[1])] = (entry[2], entry[3])
        self._newest = max((ms for ms, _ in self._kept.values()), default=0)

    def keep(self, who: str, text: str, ts: datetime, encoding: str) -> bool:
        """Remember a message; False when it is a copy, in the other encoding, of one kept."""
        key = (who, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16])
        ms = (ts - _EPOCH) // _ONE_MS
        earlier = self._kept.get(key)
        if earlier and earlier[1] != encoding and abs(ms - earlier[0]) <= _SAME_MESSAGE_MS:
            return False
        self._kept[key] = (ms, encoding)
        self._newest = max(self._newest, ms)
        cutoff = self._newest - _SAME_MESSAGE_MS
        if len(self._kept) > _MAX_RECENT or any(t < cutoff for t, _ in self._kept.values()):
            newest = sorted(((t, k) for k, (t, _) in self._kept.items() if t >= cutoff),
                            reverse=True)[:_MAX_RECENT]
            self._kept = {k: self._kept[k] for _, k in newest}
        return True

    def carry(self) -> list[list[Any]]:
        return [[who, digest, ms, encoding]
                for (who, digest), (ms, encoding) in sorted(self._kept.items())]


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

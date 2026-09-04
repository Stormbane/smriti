"""Adapter for Codex CLI session rollouts.

Source: ``~/.codex/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl``.

Kept: ``event_msg`` records with payload type ``user_message`` (Suti)
and ``agent_message`` whose phase is ``final_answer`` or absent
(Narada). ``commentary``-phase agent messages are status narration
during tool work, not conversation — dropped, as are all other record
types (``response_item``, ``session_meta``, ...).

Channel: ``codex:<project>`` from ``session_meta``'s ``cwd``, read
lazily from the file head (offset-based tailing may never revisit the
first line).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from smriti.daylog.adapters import ExtractResult, complete_lines
from smriti.daylog.model import Turn, parse_ts

_UUID_RE = re.compile(r"([0-9a-f]{8})-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")


def _session_from_name(path: Path) -> str:
    m = _UUID_RE.search(path.name)
    return m.group(1) if m else path.stem[-8:]


class CodexRolloutAdapter:
    def __init__(self) -> None:
        self._channel_cache: dict[str, str] = {}

    def _channel_for(self, path: Path) -> str:
        key = str(path)
        cached = self._channel_cache.get(key)
        if cached:
            return cached
        channel = "codex"
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                head = f.readline()
            obj = json.loads(head)
            if isinstance(obj, dict) and obj.get("type") == "session_meta":
                cwd = str((obj.get("payload") or {}).get("cwd", "") or "")
                if cwd:
                    channel = f"codex:{Path(cwd).name}"
        except (OSError, json.JSONDecodeError):
            pass
        self._channel_cache[key] = channel
        return channel

    def extract(self, path: Path, blob: bytes) -> ExtractResult:
        result = ExtractResult()
        lines, result.consumed = complete_lines(blob)
        if not lines:
            return result
        channel = self._channel_for(path)
        session = _session_from_name(path)
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
            ptype = payload.get("type")
            if ptype == "user_message":
                who = "suti"
            elif ptype == "agent_message":
                phase = payload.get("phase")
                if phase not in (None, "", "final_answer"):
                    continue
                who = "narada"
            else:
                continue
            text = payload.get("message")
            ts = parse_ts(str(obj.get("timestamp", "")))
            if ts is None or not isinstance(text, str) or not text.strip():
                result.errors += 1
                continue
            result.turns.append(
                Turn(ts=ts, channel=channel, who=who, text=text.strip(), session=session)
            )
        return result

"""Adapter for Claude Code session transcripts (and the Telegram brain).

Source: ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``. The
Telegram typed-chat brain runs as ``claude -p`` sessions, so its
transcripts arrive through this same adapter; workdirs containing
``chat-sessions`` map to the ``telegram`` channel.

Kept: real human turns (type=user, string or text-block content) and
Narada's text (type=assistant text blocks). Dropped: tool calls and
results, thinking blocks, ``isMeta`` records, sidechain (subagent)
records, harness wrapper payloads (``<command-name>``,
``<local-command-stdout>``, ``<task-notification>``,
``<system-reminder>``), and every non-message record type.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from smriti.daylog.adapters import ExtractResult, complete_lines
from smriti.daylog.model import Turn, parse_ts

_WRAPPER_RE = re.compile(
    r"^\s*<(?:command-name|command-message|command-args|local-command-stdout|"
    r"local-command-stderr|system-reminder|task-notification|ide_)"
)

# Strip inline system-reminder blocks that ride inside a genuine user turn.
_INLINE_REMINDER_RE = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>", re.MULTILINE)


def channel_for(path: Path, cwd: str) -> str:
    probe = f"{cwd} {path.parent.name}".lower()
    if "chat-sessions" in probe:
        return "telegram"
    if cwd:
        name = Path(cwd).name or "unknown"
    else:
        name = path.parent.name
    return f"claude:{name}"


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


class ClaudeJsonlAdapter:
    def extract(self, path: Path, blob: bytes) -> ExtractResult:
        result = ExtractResult()
        lines, result.consumed = complete_lines(blob)
        session = path.stem
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                result.errors += 1
                continue
            if not isinstance(obj, dict):
                result.errors += 1
                continue
            rtype = obj.get("type")
            if rtype not in ("user", "assistant"):
                continue  # progress/snapshot/summary records — not errors
            if obj.get("isMeta") or obj.get("isSidechain"):
                continue
            ts = parse_ts(str(obj.get("timestamp", "")))
            if ts is None:
                result.errors += 1
                continue
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            text = _text_from_content(content)
            text = _INLINE_REMINDER_RE.sub("", text).strip()
            if not text or _WRAPPER_RE.match(text):
                continue
            result.turns.append(
                Turn(
                    ts=ts,
                    channel=channel_for(path, str(obj.get("cwd", "") or "")),
                    who="suti" if rtype == "user" else "narada",
                    text=text,
                    session=str(obj.get("sessionId", "") or session),
                )
            )
        return result

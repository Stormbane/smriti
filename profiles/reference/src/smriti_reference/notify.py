"""The reference installation's fan-out sender: ``send``, with a level and a receipt.

The core's door to the user is ``smriti.daylog.notify.notify_user``: the
message on ``notify_cmd``'s stdin. ``send`` is the same door with a level
and a receipt: it sets ``NOTIFY_LEVEL`` for the fan-out sender
(prana's ``python -m prana.notify``: Telegram, then the brain's app feed
and push) and reports which channels delivered. The ``tell_suti`` MCP tool uses it, and
the HANDS wake section names the command.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.notify import NotifyError as NotifyError  # re-exported: send raises it

log = logging.getLogger(__name__)

# Levels the brain's feed understands (prana.brain.push.LEVELS).
# message/error/emergency also go to Telegram; log/warn are feed-only.
LEVELS: tuple[str, ...] = ("log", "warn", "message", "error", "emergency")
FEED_ONLY_LEVELS: tuple[str, ...] = ("log", "warn")

# Senders that honour NOTIFY_LEVEL, as the module a ``-m`` runs. A
# single-channel command such as ``-m prana.notify.telegram`` ignores it and
# would put feed-only levels on Telegram, so feed-only levels are refused
# unless one of these is the command.
LEVEL_AWARE_SENDERS: frozenset[str] = frozenset({"prana.notify"})

# prana's Telegram sender cuts at 4,000 (Telegram's hard limit is 4,096).
# Refuse rather than let a message arrive silently truncated.
MAX_MESSAGE_CHARS = 4000

_DELIVERED_RE = re.compile(r"^delivered via:\s*(.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Delivery:
    """Which channels took the message, plus the sender's own log tail."""

    channels: tuple[str, ...]
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.channels)


# (text, level) -> Delivery. Injectable so tests never send for real.
Sender = Callable[[str, str], Delivery]


def notify_command(cfg: DaylogConfig) -> list[str]:
    """The command that reaches Suti: daylog.json's notify_cmd, or nothing.

    Never a script in the tree, which a write to the tree could change."""
    return list(cfg.notify_cmd or [])


def level_aware(cmd: list[str]) -> bool:
    """Whether *cmd* runs a sender that honours ``NOTIFY_LEVEL``."""
    return any(
        flag == "-m" and module in LEVEL_AWARE_SENDERS
        for flag, module in zip(cmd, cmd[1:], strict=False)
    )


def parse_channels(stderr: str) -> tuple[str, ...] | None:
    """Channels from the fan-out sender's last ``delivered via: ...`` line.
    None when the sender printed no such line (an older single-channel
    command); an empty tuple when it said NOTHING."""
    matches = _DELIVERED_RE.findall(stderr or "")
    if not matches:
        return None
    last = matches[-1].strip()
    if not last or last.upper() == "NOTHING":
        return ()
    return tuple(part.strip() for part in last.split(",") if part.strip())


def send(
    cfg: DaylogConfig,
    message: str,
    *,
    level: str = "message",
    source: str = "tell_suti",
    timeout_s: int = 120,
) -> Delivery:
    """Pipe *message* to the notify command with ``NOTIFY_LEVEL`` set.

    Raises :class:`NotifyError` when nothing is configured, the level is
    unknown, the message is empty or too long, or no channel delivered.
    """
    text = (message or "").strip()
    if not text:
        raise NotifyError("empty message — nothing sent")
    if len(text) > MAX_MESSAGE_CHARS:
        raise NotifyError(
            f"message is {len(text)} chars; the limit is {MAX_MESSAGE_CHARS}. Split it."
        )
    if level not in LEVELS:
        raise NotifyError(f"unknown level {level!r}; use one of {', '.join(LEVELS)}")
    cmd = notify_command(cfg)
    if not cmd:
        raise NotifyError("no notify_cmd in .smriti/daylog.json")
    if level in FEED_ONLY_LEVELS and not level_aware(cmd):
        raise NotifyError(
            f"level {level!r} is feed-only, but the notify command "
            f"({Path(cmd[-1]).name}) ignores levels and would send it to Telegram. "
            "Use level='message', or point notify_cmd at python -m prana.notify."
        )
    env = {
        **os.environ,
        "NOTIFY_LEVEL": level,
        "NOTIFY_SOURCE": source,
        # The child decodes stdin with the locale codepage on Windows
        # (cp1252) without this.
        "PYTHONUTF8": "1",
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotifyError(f"notify command timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise NotifyError(f"cannot run notify command: {exc}") from exc
    detail = (result.stderr or "").strip()[-600:]
    channels = parse_channels(result.stderr or "")
    if result.returncode != 0:
        raise NotifyError(f"not delivered (exit {result.returncode}): {detail or '(no output)'}")
    if channels is None:
        # Exit 0 from a command that doesn't report channels (e.g. the
        # older notify_telegram.py wiring): delivered, channel unnamed.
        channels = ("notify_cmd",)
    if not channels:
        raise NotifyError(f"not delivered: {detail or 'sender reported NOTHING'}")
    return Delivery(channels=channels, detail=detail)

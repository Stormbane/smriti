"""``notify_user`` — the outbound shim.

Today it runs the configured command (``notify_cmd`` in
``<tree>/.smriti/daylog.json``) with the message on stdin — wired to
the Telegram gateway at install time. This function is the designed
first call site for the future presence-routing tool (spec: Future):
callers express intent to reach the user; HOW is this shim's problem.

``send`` is the same door with a level and a receipt: it sets
``NOTIFY_LEVEL`` for the fan-out sender (``notify_suti.py``: Telegram,
then the brain's app feed and push) and reports which channels
delivered. The ``tell_suti`` MCP tool uses it.
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

log = logging.getLogger(__name__)

# Levels the brain's feed understands (prana.brain.push.LEVELS).
# message/error/emergency also go to Telegram; log/warn are feed-only.
LEVELS: tuple[str, ...] = ("log", "warn", "message", "error", "emergency")
FEED_ONLY_LEVELS: tuple[str, ...] = ("log", "warn")

# Senders that honour NOTIFY_LEVEL. A legacy command such as
# notify_telegram.py ignores it and would put feed-only levels on Telegram,
# so feed-only levels are refused unless one of these is the command.
LEVEL_AWARE_SENDERS: frozenset[str] = frozenset({"notify_suti.py"})

# The fan-out sender, relative to the tree root. Used when daylog.json
# names no notify_cmd.
NOTIFY_SCRIPT = Path(".smriti") / "hooks" / "notify_suti.py"

# notify_telegram.py cuts at 4,000 (Telegram's hard limit is 4,096).
# Refuse rather than let a message arrive silently truncated.
MAX_MESSAGE_CHARS = 4000

_DELIVERED_RE = re.compile(r"^delivered via:\s*(.*?)\s*$", re.MULTILINE)


class NotifyError(RuntimeError):
    """Delivery failed or no channel is configured."""


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


def notify_user(cfg: DaylogConfig, message: str, *, timeout_s: int = 60) -> None:
    """Pipe *message* to ``notify_cmd`` on stdin; raises :class:`NotifyError` on failure."""
    if not cfg.notify_cmd:
        raise NotifyError(
            "no notify_cmd configured in .smriti/daylog.json — message not sent"
        )
    result = subprocess.run(
        cfg.notify_cmd,
        input=message,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "(no output)")[:300]
        raise NotifyError(f"notify_cmd exit {result.returncode}: {snippet}")


def notify_command(cfg: DaylogConfig) -> list[str]:
    """The command that reaches Suti: daylog.json's notify_cmd, else the
    fan-out script in the tree, else nothing."""
    if cfg.notify_cmd:
        return list(cfg.notify_cmd)
    script = cfg.root / NOTIFY_SCRIPT
    if script.is_file():
        return [sys.executable, str(script)]
    return []


def parse_channels(stderr: str) -> tuple[str, ...] | None:
    """Channels from notify_suti.py's last ``delivered via: ...`` line.
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
        raise NotifyError(
            f"no notify_cmd in .smriti/daylog.json and no {cfg.root / NOTIFY_SCRIPT}"
        )
    if level in FEED_ONLY_LEVELS and not any(
        Path(part).name in LEVEL_AWARE_SENDERS for part in cmd
    ):
        raise NotifyError(
            f"level {level!r} is feed-only, but the notify command "
            f"({Path(cmd[-1]).name}) ignores levels and would send it to Telegram. "
            "Use level='message', or point notify_cmd at notify_suti.py."
        )
    env = {
        **os.environ,
        "NOTIFY_LEVEL": level,
        "NOTIFY_SOURCE": source,
        # The child decodes stdin with the locale codepage on Windows
        # (cp1252) without this; see notify_suti.py.
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

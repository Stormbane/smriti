"""``notify_suti`` — the outbound shim.

Today it runs the configured command (``notify_cmd`` in
``<tree>/.smriti/daylog.json``) with the message on stdin — wired to
the Telegram gateway at install time. This function is the designed
first call site for the future presence-routing tool (spec: Future):
callers express intent to reach Suti; HOW is this shim's problem.
"""

from __future__ import annotations

import logging
import subprocess

from smriti.daylog.config import DaylogConfig

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    """Delivery failed or no channel is configured."""


def notify_suti(cfg: DaylogConfig, message: str, *, timeout_s: int = 60) -> None:
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

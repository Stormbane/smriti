"""``notify_user`` — the outbound shim.

Today it runs the configured command (``notify_cmd`` in
``<tree>/.smriti/daylog.json``) with the message on stdin — wired to
the Telegram gateway at install time. This function is the designed
first call site for the future presence-routing tool (spec: Future):
callers express intent to reach the user; HOW is this shim's problem.

A profile may add a richer sender: the reference profile's
``smriti_reference.notify.send`` (in its own package, ``profiles/reference/``)
adds levels and a delivery receipt, for its ``tell_suti`` tool.
"""

from __future__ import annotations

import subprocess

from smriti.daylog.config import DaylogConfig


class NotifyError(RuntimeError):
    """Delivery failed or no channel is configured."""


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
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "(no output)")[:300]
        raise NotifyError(f"notify_cmd exit {result.returncode}: {snippet}")

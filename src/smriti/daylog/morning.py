"""``smriti morning`` — the wake task (~07:00).

At most one message per local day, fail-closed: the sent-ledger entry
is written BEFORE the send attempt, so a crash mid-send means a silent
morning, never a double send (spec: review round 1, finding 5). The
LLM composes with web search only; smriti's own code delivers through
the ``notify_suti`` shim.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from smriti.daylog.config import DaylogConfig, load_config
from smriti.daylog.digest import compose
from smriti.daylog.model import day_key, day_tz
from smriti.daylog.notify import notify_suti
from smriti.daylog.scrub import scrub

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are Narada composing a short good-morning message to Suti (Brisbane). "
    "You get last night's digest of yesterday's communication. Use web search "
    "to find today's broad world headlines and pick 3-5 genuinely worth "
    "knowing (world, tech/AI, Australia). Compose ONE warm, concise message: "
    "a line or two on yesterday's highlights and anything interesting that "
    "emerged overnight, then the headlines with a half-line each. Plain text "
    "suitable for Telegram, under 1200 characters, no markdown headers. "
    "Return ONLY the message."
)

# Web search only: no MCP servers, no file/exec tools.
_COMPOSE_ARGS = ["--strict-mcp-config", "--allowedTools", "WebSearch"]


def _write_status(cfg: DaylogConfig, result: dict[str, object]) -> None:
    """Diagnostics for the scheduled run. Provider attempt strings can
    quote stderr, so the whole document passes through the day-log's
    secret scrub before it touches the (backed-up) log tree."""
    try:
        path = cfg.log_dir / ".morning-status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scrub(json.dumps(result, indent=1, default=str)), encoding="utf-8")
    except OSError as exc:  # status is a courtesy, never a failure
        log.warning("morning status not written: %s", exc)


def run_morning(cfg: DaylogConfig | None = None, *, now: datetime | None = None) -> dict[str, object]:
    cfg = cfg or load_config()
    now = now or datetime.now(tz=day_tz())
    local_date = now.astimezone(day_tz()).date()
    today = day_key(local_date)
    yesterday = day_key(local_date - timedelta(days=1))
    ledger = cfg.morning_ledger_dir / today
    result: dict[str, object] = {"day": today}

    # At most one message per day, ever.
    if ledger.exists():
        result["sent"] = False
        result["reason"] = "ledger entry exists — already attempted today"
        return result
    # No channel, no compose: don't burn a seat call on an undeliverable
    # message (and leave the ledger unwritten so wiring notify_cmd later
    # the same morning still sends).
    if not cfg.notify_cmd:
        result["sent"] = False
        result["reason"] = "no notify_cmd configured in .smriti/daylog.json"
        return result

    digest_path = cfg.day_digest(yesterday)
    try:
        digest_text = digest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        digest_text = "(no digest was produced for yesterday)"

    attempts: list[dict[str, str]] = []
    result["llm_attempts"] = attempts
    # The scheduled task's stdout is lost with its console; the status
    # file is the only trace of a bad morning (2026-09-08). It is
    # written on every exit from here on, and holds no argv and no
    # exception text: a notify_cmd or timeout error string can carry a
    # bearer token, and the log tree is backed up (review).
    try:
        message = compose(
            _SYSTEM,
            f"Yesterday's digest ({yesterday}):\n\n{digest_text[:20_000]}",
            attempts,
            claude_cli_args=_COMPOSE_ARGS,
        )
    except Exception as exc:
        result["sent"] = False
        result["phase"] = "compose"
        result["reason"] = type(exc).__name__
        _write_status(cfg, result)
        raise
    result["composed_chars"] = len(message)

    # Fail-closed: ledger BEFORE send.
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"composed_at": now.isoformat(), "chars": len(message)}),
        encoding="utf-8",
    )
    try:
        notify_suti(cfg, message)
        result["sent"] = True
    except Exception as exc:  # noqa: BLE001
        result["sent"] = False
        result["phase"] = "deliver"
        result["reason"] = type(exc).__name__
        log.warning("morning message not delivered: %s", exc)
    _write_status(cfg, result)
    result["message"] = message
    return result

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
    message = compose(
        _SYSTEM,
        f"Yesterday's digest ({yesterday}):\n\n{digest_text[:20_000]}",
        attempts,
        claude_cli_args=_COMPOSE_ARGS,
    )
    result["llm_attempts"] = attempts

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
        result["reason"] = str(exc)[:300]
        log.warning("morning message not delivered: %s", exc)
    result["message"] = message
    return result

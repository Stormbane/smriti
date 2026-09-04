"""The daily digest — one read-compose-emit LLM call per day.

Primary seat: ``claude_cli`` (Suti's ruling 2026-09-04); fallback:
``codex_cli``. The subprocess is constrained to no tools/MCP — it holds
no ``smriti_write`` license; anything notable belongs in the digest
file itself, which lives in the tree and gets indexed (spec: review
round 1, finding 6).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from smriti.daylog.config import DaylogConfig
from smriti.daylog.model import Turn, day_tz
from smriti.daylog.render import input_hash
from smriti.daylog.writer import read_day_turns
from smriti.llm import LLMError, LLMRequest, get_provider

log = logging.getLogger(__name__)

# Argv/context budget for the compose prompt.
_MAX_LOG_CHARS = 60_000
_MAX_AUTON_CHARS = 8_000

_SYSTEM = (
    "You are Narada's nightly digest writer. You receive one day's complete "
    "communication log between Suti and Narada across every channel (voice, "
    "Telegram, Claude Code sessions, Codex), plus a sample of autonomous "
    "session activity. Write a compact digest in markdown:\n"
    "- '## Highlights' — the 3-8 moments that mattered, with times;\n"
    "- '## Decisions' — anything decided or ruled, verbatim where short;\n"
    "- '## Threads' — open questions and things waiting on someone;\n"
    "- '## Autonomous' — one short paragraph on unattended activity, if any.\n"
    "Be specific and faithful to the log; omit empty sections; no preamble."
)

# No-tools constraint for the compose subprocess. --strict-mcp-config with
# no --mcp-config drops MCP servers entirely.
_NO_TOOLS_ARGS = ["--strict-mcp-config", "--disallowedTools", "*"]


def _providers() -> list[str]:
    primary = os.environ.get("SMRITI_NIGHTLY_PROVIDER", "claude_cli")
    fallback = os.environ.get("SMRITI_NIGHTLY_FALLBACK", "codex_cli")
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def compose(
    system: str,
    user: str,
    attempts_log: list[dict[str, str]],
    *,
    claude_cli_args: list[str] | None = None,
) -> str:
    """Run one read-compose-emit call through the provider chain.

    ``claude_cli_args`` overrides the default no-tools constraint for
    the claude_cli seat (the morning call allows web search).
    """
    last_error: Exception | None = None
    for name in _providers():
        try:
            provider = get_provider(name)
            if not provider.is_available():
                attempts_log.append({"provider": name, "outcome": "unavailable"})
                continue
            cli_args = (claude_cli_args or _NO_TOOLS_ARGS) if name == "claude_cli" else None
            response = provider.call(
                LLMRequest(system=system, user=user, max_tokens=4096, cli_args=cli_args)
            )
            attempts_log.append({"provider": name, "outcome": "ok"})
            return response.text.strip()
        except Exception as exc:  # noqa: BLE001 — record and try the fallback
            attempts_log.append({"provider": name, "outcome": f"error: {exc}"[:200]})
            last_error = exc
            log.warning("digest provider %s failed: %s", name, exc)
    raise LLMError(f"all providers failed: {last_error}")


def _autonomous_sample(turns: list[Turn]) -> str:
    by_session: dict[tuple[str, str], list[Turn]] = {}
    for t in turns:
        by_session.setdefault((t.channel, t.session), []).append(t)
    parts: list[str] = []
    for (channel, _session), group in sorted(by_session.items(), key=lambda kv: kv[1][0].ts):
        if any(t.who == "suti" for t in group):
            continue
        sample = group[:3] + (group[-2:] if len(group) > 5 else [])
        parts.append(f"[{channel}] " + " / ".join(t.text[:200] for t in sample))
    return "\n".join(parts)[:_MAX_AUTON_CHARS]


def digest_day(cfg: DaylogConfig, day: str, attempts_log: list[dict[str, str]]) -> Path | None:
    """Write ``DD-digest.md`` for *day*. Returns None if there is no log."""
    turns = read_day_turns(cfg.day_jsonl(day))
    if not turns:
        return None
    rendered = cfg.day_md(day)
    try:
        log_text = rendered.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    user = f"Day log for {day}:\n\n{log_text[:_MAX_LOG_CHARS]}"
    autonomous = _autonomous_sample(turns)
    if autonomous:
        user += f"\n\nAutonomous activity sample:\n{autonomous}"

    text = compose(_SYSTEM, user, attempts_log)

    out = cfg.day_digest(day)
    tz = day_tz()
    header = "\n".join(
        [
            "---",
            f"date: {day}",
            f"input_hash: {input_hash(turns)}",
            f"tz_offset: {tz.utcoffset(None)}",
            "generated_by: smriti nightly (digest)",
            "---",
            "",
        ]
    )
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(header + text + "\n", encoding="utf-8")
    tmp.replace(out)
    return out

"""Read-only readiness checks for a Smriti harness installation."""

from __future__ import annotations

import importlib.util
import tomllib
from dataclasses import dataclass
from pathlib import Path

from smriti.integrations.common import SMRITI_MCP_COMMAND, make_wake_hook_command
from smriti.wake import briefing


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def codex_checks(
    *, memory_root: Path, project: Path, home: Path | None = None
) -> list[Check]:
    """Validate the installed Codex bridge without changing user state."""
    home = home or Path.home()
    config_path = home / ".codex" / "config.toml"
    agents_path = home / ".codex" / "AGENTS.md"
    checks: list[Check] = []

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as exc:
        checks.append(Check("Codex config", False, str(exc)))
        config = {}
    else:
        expected_wake = make_wake_hook_command(
            memory_root, home=home, framing="codex-json", audience="coding"
        )
        hooks = config.get("hooks", {}).get("SessionStart", [])
        hook_ok = any(
            group.get("matcher") == "startup|resume|clear|compact"
            and any(hook.get("command") == expected_wake for hook in group.get("hooks", []))
            for group in hooks
        )
        checks.append(Check("Codex wake hook", hook_ok, "startup/resume/clear/compact"))
        checks.append(
            Check("Codex hooks enabled", config.get("features", {}).get("hooks") is True, "[features].hooks")
        )
        checks.append(
            Check(
                "Smriti MCP registration",
                config.get("mcp_servers", {}).get("smriti") == SMRITI_MCP_COMMAND,
                "python -m smriti.mcp_server",
            )
        )

    try:
        agents = agents_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        checks.append(Check("Global AGENTS.md", False, str(exc)))
    else:
        checks.append(Check("Global AGENTS.md", "Memory system" in agents, str(agents_path)))

    checks.append(
        Check("Smriti MCP module", importlib.util.find_spec("smriti.mcp_server") is not None, "importable")
    )
    payload = briefing(memory_root=memory_root, cwd=project, audience="coding")
    checks.append(Check("Narada core context", "I am Narada." in payload, "wake briefing"))
    checks.append(
        Check("Hermes routing excluded", "You are Narada's **receptionist**" not in payload, "coding audience")
    )
    checks.append(
        Check("Canonical project ledger", "STATUS" in payload and "INDEX" in payload, str(project / ".ai"))
    )
    return checks


def run_doctor(*, harness: str, memory_root: Path, project: Path) -> int:
    if harness != "codex":
        print(f"Unsupported harness: {harness}. Supported: codex")
        return 2
    checks = codex_checks(memory_root=memory_root, project=project)
    for check in checks:
        marker = "PASS" if check.passed else "FAIL"
        print(f"[{marker}] {check.name} — {check.detail}")
    return 0 if all(check.passed for check in checks) else 1

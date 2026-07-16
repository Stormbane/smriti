"""Read-only readiness checks for a Smriti harness installation.

Checks are *semantic*, not string-literal: the wake-hook check runs the
same canonical classifier the installers use (so a PowerShell
``-EncodedCommand`` wake hook on Windows passes exactly when it would
wake correctly), and the MCP check is a superset match (harness
annotations like Codex's ``tools.*`` approval subtables don't read as
"not registered"). Deployed-hook integrity and hook-target existence
run for BOTH harnesses — the shims swallow their own failures by
design, so drift is invisible at runtime and doctor is where it must
surface.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from smriti.integrations.common import (
    EQUIVALENT,
    SHARED_HOOKS_DIR,
    check_deployed_hooks,
    classify_doc,
    classify_wake_hook,
    mcp_registration_matches,
)
from smriti.integrations.common.hook_model import _ENCODED_RE  # noqa: F401 (reused below)
from smriti.wake import briefing


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


# --- shared helpers -------------------------------------------------------

_SH_SCRIPT_RE = re.compile(r'python3?\s+"?((?:\$HOME|~)/[^"\s]+\.py)"?')
_PS_SCRIPT_RE = re.compile(r"\(Join-Path \$HOME '([^']+\.py)'\)")


def _script_targets(command: str, home: Path) -> list[Path]:
    """Resolve every ``python <script>.py`` target a hook command runs."""
    import base64

    targets: list[Path] = []
    encoded = _ENCODED_RE.search(command)
    if encoded:
        try:
            script = base64.b64decode(encoded.group(1)).decode("utf-16-le")
        except (ValueError, UnicodeDecodeError):
            return []
        for rel in _PS_SCRIPT_RE.findall(script):
            targets.append(home / rel.replace("\\", "/").strip("/"))
        return targets
    for raw in _SH_SCRIPT_RE.findall(command):
        rel = raw.replace("$HOME/", "").replace("~/", "")
        targets.append(home / rel)
    return targets


def _all_hook_commands(hooks: dict) -> list[str]:
    """Every command string under list-shaped hook-event entries.

    Skips non-event tables that share the ``hooks`` namespace (Codex
    keeps a ``[hooks.state]`` trust registry there) and any entry that
    is not dict-shaped.
    """
    return [
        hook.get("command", "")
        for groups in hooks.values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    ]


def _hook_targets_check(
    name: str, commands: list[str], home: Path
) -> Check:
    """Every hook command must point at a script that exists on disk.

    Hooks fail silent by design, so a repointed-then-deleted script is
    otherwise invisible.
    """
    missing = [
        str(target)
        for command in commands
        for target in _script_targets(command, home)
        if not target.exists()
    ]
    if missing:
        return Check(name, False, "missing: " + ", ".join(missing))
    return Check(name, True, f"{len(commands)} hook command(s) verified")


def _wake_hook_check(
    name: str,
    commands: list[str],
    *,
    memory_root: Path,
    home: Path,
    framing: str,
) -> Check:
    """PASS when any hook command classifies EQUIVALENT; report the most
    specific gap otherwise (a DEFICIENT detail beats "none found")."""
    best_detail = "no wake hook found"
    for command in commands:
        verdict, detail = classify_wake_hook(
            command, memory_root=memory_root, home=home, framing=framing
        )
        if verdict == EQUIVALENT:
            return Check(name, True, detail)
        if verdict != "unrelated":
            best_detail = detail
    return Check(name, False, best_detail)


def _managed_doc_check(name: str, path: Path) -> Check:
    state = classify_doc(path)
    return Check(name, state.kind == "managed", f"{path}: {state.detail}")


def _briefing_checks(memory_root: Path, project: Path) -> list[Check]:
    payload = briefing(memory_root=memory_root, cwd=project, audience="coding")
    return [
        Check("Narada core context", "I am Narada." in payload, "wake briefing"),
        Check(
            "Hermes routing excluded",
            "You are Narada's **receptionist**" not in payload,
            "coding audience",
        ),
        Check(
            "Canonical project ledger",
            "STATUS" in payload and "INDEX" in payload,
            str(project / ".ai"),
        ),
    ]


def _mcp_module_check() -> Check:
    return Check(
        "Smriti MCP module",
        importlib.util.find_spec("smriti.mcp_server") is not None,
        "importable",
    )


# --- codex ----------------------------------------------------------------

def codex_checks(
    *, memory_root: Path, project: Path, home: Path | None = None
) -> list[Check]:
    """Validate the installed Codex bridge without changing user state."""
    home = home or Path.home()
    codex = home / ".codex"
    config_path = codex / "config.toml"
    checks: list[Check] = []

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as exc:
        checks.append(Check("Codex config", False, str(exc)))
        config = {}
    else:
        hooks = config.get("hooks", {})
        session_cmds = [
            hook.get("command", "")
            for group in hooks.get("SessionStart", [])
            if group.get("matcher") == "startup|resume|clear|compact"
            for hook in group.get("hooks", [])
        ]
        checks.append(
            _wake_hook_check(
                "Codex wake hook",
                session_cmds,
                memory_root=memory_root,
                home=home,
                framing="codex-json",
            )
        )
        checks.append(
            Check(
                "Codex hooks enabled",
                config.get("features", {}).get("hooks") is True,
                "[features].hooks",
            )
        )
        checks.append(
            Check(
                "Smriti MCP registration",
                mcp_registration_matches(config.get("mcp_servers", {}).get("smriti")),
                "python -m smriti.mcp_server",
            )
        )
        # hooks holds event arrays AND non-event tables (Codex writes a
        # [hooks.state] trust registry) — only walk the list-shaped ones.
        all_cmds = _all_hook_commands(hooks)
        checks.append(_hook_targets_check("Codex hook targets exist", all_cmds, home))

    checks.append(_managed_doc_check("Global AGENTS.md", codex / "AGENTS.md"))

    problems = check_deployed_hooks(
        SHARED_HOOKS_DIR, codex / "hooks", {"recall_shim.py": "recall_hook.py"}
    )
    checks.append(
        Check(
            "Codex deployed hooks",
            not problems,
            "; ".join(problems) or "byte-match with source",
        )
    )

    checks.append(_mcp_module_check())
    checks.extend(_briefing_checks(memory_root, project))
    return checks


# --- claude code ----------------------------------------------------------

def claude_code_checks(
    *, memory_root: Path, project: Path, home: Path | None = None
) -> list[Check]:
    """Validate the installed Claude Code bridge without changing state."""
    home = home or Path.home()
    claude = home / ".claude"
    settings_path = claude / "settings.json"
    checks: list[Check] = []

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        checks.append(Check("Claude settings", False, str(exc)))
        settings = {}
    else:
        hooks = settings.get("hooks", {})
        session_cmds = [
            hook.get("command", "")
            for group in hooks.get("SessionStart", [])
            for hook in group.get("hooks", [])
        ]
        checks.append(
            _wake_hook_check(
                "Claude wake hook",
                session_cmds,
                memory_root=memory_root,
                home=home,
                framing="raw",
            )
        )
        all_cmds = _all_hook_commands(hooks)
        checks.append(_hook_targets_check("Claude hook targets exist", all_cmds, home))

    claude_config = home / ".claude.json"
    try:
        registry = json.loads(claude_config.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        checks.append(Check("Smriti MCP registration", False, str(exc)))
    else:
        checks.append(
            Check(
                "Smriti MCP registration",
                mcp_registration_matches(
                    registry.get("mcpServers", {}).get("smriti")
                ),
                "python -m smriti.mcp_server",
            )
        )

    checks.append(_managed_doc_check("Global CLAUDE.md", claude / "CLAUDE.md"))

    from smriti.integrations.claude_code import install as claude_install

    problems = check_deployed_hooks(
        SHARED_HOOKS_DIR, claude / "hooks", claude_install.HOOK_DEPLOY_MAP
    )
    problems += check_deployed_hooks(
        claude_install.HOOKS_SRC, claude / "hooks", claude_install.LOCAL_HOOKS
    )
    checks.append(
        Check(
            "Claude deployed hooks",
            not problems,
            "; ".join(problems) or "byte-match with source",
        )
    )

    checks.append(_mcp_module_check())
    checks.extend(_briefing_checks(memory_root, project))
    return checks


# --- entrypoint -----------------------------------------------------------

_HARNESS_CHECKS = {
    "codex": codex_checks,
    "claude-code": claude_code_checks,
    "claude_code": claude_code_checks,
}


def run_doctor(*, harness: str, memory_root: Path, project: Path) -> int:
    check_fn = _HARNESS_CHECKS.get(harness)
    if check_fn is None:
        print(f"Unsupported harness: {harness}. Supported: codex, claude-code")
        return 2
    checks = check_fn(memory_root=memory_root, project=project)
    for check in checks:
        marker = "PASS" if check.passed else "FAIL"
        print(f"[{marker}] {check.name} — {check.detail}")
    return 0 if all(check.passed for check in checks) else 1

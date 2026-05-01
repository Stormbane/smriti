"""Claude-Code-specific install steps.

Wires smriti into Anthropic's Claude Code CLI:
    - Patches ``~/.claude/settings.json`` with SessionStart wake,
      UserPromptSubmit activity touch, and PostToolUse recall hooks.
    - Registers smriti's MCP server in ``~/.claude.json`` (user scope).
    - Drops ``~/.claude/CLAUDE.md`` composed from the harness-neutral
      ``smriti/templates/AGENT.md`` plus a Claude-Code-specific addendum.
    - Deploys hook scripts from this package's ``hooks/`` dir into
      ``~/.claude/hooks/``.

Shared helpers — MCP spec, AGENT.md composition, hook deployment —
come from ``smriti.integrations.common`` so any change to those
pieces flows to every harness adapter at once. Only the Claude-Code-
specific config-file format and the hook-command shape live here.

All operations are idempotent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from smriti.integrations.common import (
    SMRITI_MCP_COMMAND,
    compose_agent_doc,
    deploy_hook_scripts,
    make_wake_hook_command,
)

HOME = Path.home()
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
CLAUDE_MD = CLAUDE / "CLAUDE.md"
CLAUDE_CONFIG = HOME / ".claude.json"  # MCP server registry
HOOKS_DST = CLAUDE / "hooks"
HOOKS_SRC = Path(__file__).resolve().parent / "hooks"


def register_mcp_server() -> None:
    """Add the smriti MCP server to ``~/.claude.json`` at user scope."""
    if not CLAUDE_CONFIG.exists():
        print(f"[mcp] {CLAUDE_CONFIG} not found — skipping (Claude Code not run yet?)")
        return
    try:
        data = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[mcp] parse error on {CLAUDE_CONFIG}: {exc}; leaving untouched")
        return

    servers = data.setdefault("mcpServers", {})
    if servers.get("smriti") == SMRITI_MCP_COMMAND:
        print("[mcp] smriti server already registered")
        return
    servers["smriti"] = dict(SMRITI_MCP_COMMAND)
    backup = CLAUDE_CONFIG.with_suffix(".json.bak")
    shutil.copy2(CLAUDE_CONFIG, backup)
    CLAUDE_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[mcp] registered smriti server (backup: {backup})")


def patch_settings_json(memory_root: Path) -> None:
    """Wire SessionStart / UserPromptSubmit / PostToolUse hooks."""
    if not SETTINGS.exists():
        print(f"[settings] {SETTINGS} not found — skipping")
        return
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[settings] parse error: {exc}; leaving untouched")
        return

    hooks = data.setdefault("hooks", {})
    memory_rel = memory_root.relative_to(HOME).as_posix()
    wake_cmd = make_wake_hook_command(memory_root, home=HOME, framing="raw")
    activity_cmd = (
        f'touch "$HOME/{memory_rel}/.smriti/last-activity" 2>/dev/null || true'
    )
    recall_cmd = "python ~/.claude/hooks/associative_recall.py"

    changed = False

    # SessionStart: wake.py
    session_start = hooks.get("SessionStart", [])
    wake_wired = any(
        any(h.get("command") == wake_cmd for h in group.get("hooks", []))
        for group in session_start
    )
    if wake_wired:
        print("[settings] SessionStart wake hook already wired")
    else:
        hooks["SessionStart"] = [
            {"matcher": "", "hooks": [{"type": "command", "command": wake_cmd}]}
        ]
        print("[settings] SessionStart -> wake.py")
        changed = True

    # UserPromptSubmit: touch last-activity (additive)
    ups = hooks.setdefault("UserPromptSubmit", [])
    activity_wired = any(
        any(h.get("command") == activity_cmd for h in group.get("hooks", []))
        for group in ups
    )
    if activity_wired:
        print("[settings] UserPromptSubmit activity hook already wired")
    else:
        if ups and ups[0].get("matcher", "") == "":
            ups[0].setdefault("hooks", []).append(
                {"type": "command", "command": activity_cmd}
            )
        else:
            ups.append(
                {"matcher": "", "hooks": [{"type": "command", "command": activity_cmd}]}
            )
        print("[settings] UserPromptSubmit -> touch last-activity")
        changed = True

    # PostToolUse: associative recall on Read|Edit|Write
    post = hooks.setdefault("PostToolUse", [])
    recall_wired = any(
        any(h.get("command") == recall_cmd for h in group.get("hooks", []))
        for group in post
    )
    if recall_wired:
        print("[settings] PostToolUse recall hook already wired")
    else:
        post.append({
            "matcher": "Read|Edit|Write",
            "hooks": [{"type": "command", "command": recall_cmd}],
        })
        print("[settings] PostToolUse -> associative_recall.py")
        changed = True

    if not changed:
        return

    backup = SETTINGS.with_suffix(".json.bak")
    shutil.copy2(SETTINGS, backup)
    SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[settings] saved (backup: {backup})")


def install_hook_scripts() -> None:
    """Deploy hooks from this package into ``~/.claude/hooks/``."""
    deploy_hook_scripts(HOOKS_SRC, HOOKS_DST, ["associative_recall.py"])


# Claude-Code-specific addendum appended after the generic AGENT.md
# body. Speaks to the SessionStart hook, the ~/.claude/CLAUDE.md
# location, and SMRITI_WAKE gating — things only this harness has.
CLAUDE_CODE_ADDENDUM = """## Session wake (Claude Code)

On SessionStart, `{memory_rel}/.smriti/wake.py` runs via the hook
configured in `~/.claude/settings.json`. It is silent unless
`SMRITI_WAKE=1` is set in its environment — the SessionStart hook sets
this so interactive sessions wake fully, while `claude -p` and other
headless callers stay clean. The hook output is truncated at 10,000
characters by Claude Code, so the briefing budget defaults to 9,500.

## Memory search — prefer smriti_read over Grep

Ambient recall is wired on `Read|Edit|Write` via the PostToolUse hook
in `~/.claude/settings.json` — relevant memory is injected as a
system-reminder when you touch files. Call `smriti_read` yourself for
the cases listed above where the hook can't see your intent.

Use plain Grep on the memory tree only for literal string match.
"""


def write_claude_md(memory_root: Path) -> None:
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    content = compose_agent_doc(
        addendum=CLAUDE_CODE_ADDENDUM,
        memory_rel=memory_rel,
        header="# CLAUDE.md (user-global)",
    )
    CLAUDE.mkdir(parents=True, exist_ok=True)
    if CLAUDE_MD.exists() and CLAUDE_MD.read_text(encoding="utf-8") == content:
        print(f"[CLAUDE.md] {CLAUDE_MD} up to date")
        return
    CLAUDE_MD.write_text(content, encoding="utf-8")
    print(f"[CLAUDE.md] wrote {CLAUDE_MD}")


def run_claude_code(
    memory_root: Path,
    *,
    skip_settings: bool = False,
    skip_mcp: bool = False,
) -> None:
    """Run all Claude-Code-specific install steps. Idempotent."""
    install_hook_scripts()
    if not skip_mcp:
        register_mcp_server()
    if not skip_settings:
        patch_settings_json(memory_root)
    write_claude_md(memory_root)

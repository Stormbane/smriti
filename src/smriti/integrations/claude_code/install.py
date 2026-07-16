"""Claude-Code-specific install steps.

Wires smriti into Anthropic's Claude Code CLI:
    - Patches ``~/.claude/settings.json`` with SessionStart wake,
      UserPromptSubmit activity touch, and PostToolUse recall hooks.
    - Registers smriti's MCP server in ``~/.claude.json`` (user scope).
    - Maintains the smriti-managed block in ``~/.claude/CLAUDE.md``
      (composed from ``smriti/templates/AGENT.md`` plus a Claude-Code-
      specific addendum). Content outside the marker pair is the
      user's and is never touched; unmarked legacy files are refused
      at preflight — before ANY other mutation — and migrated once via
      ``--migrate-agent-doc``.
    - Deploys hook shims into ``~/.claude/hooks/``.

Shared helpers — MCP spec, managed-doc machinery, hook model, hook
deployment — come from ``smriti.integrations.common`` so any change to
those pieces flows to every harness adapter at once. Only the Claude-
Code-specific config-file format and addendum text live here.

All operations are idempotent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from smriti.integrations.common import (
    DEFICIENT,
    EQUIVALENT,
    SHARED_HOOKS_DIR,
    SMRITI_MCP_COMMAND,
    ManagedDocError,
    classify_doc,
    classify_wake_hook,
    compose_agent_doc,
    deploy_hook_scripts,
    make_wake_hook_command,
    mcp_registration_matches,
    migrate_agent_doc,
    write_managed_doc,
)

HOME = Path.home()
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
CLAUDE_MD = CLAUDE / "CLAUDE.md"
CLAUDE_CONFIG = HOME / ".claude.json"  # MCP server registry
HOOKS_DST = CLAUDE / "hooks"
HOOKS_SRC = Path(__file__).resolve().parent / "hooks"

# Claude Code runs hook commands under a POSIX shell on every platform
# (unlike Codex, which uses PowerShell on Windows — see codex/install.py).
WAKE_STYLE = "sh"
WAKE_FRAMING = "raw"

# One shared recall shim, deployed under this harness's local filename.
HOOK_DEPLOY_MAP = {"recall_shim.py": "associative_recall.py"}
# Claude-Code-only hooks that stay in this package.
LOCAL_HOOKS = ["precompact_capture.py"]


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
    if mcp_registration_matches(servers.get("smriti")):
        print("[mcp] smriti server already registered")
        return
    servers["smriti"] = dict(SMRITI_MCP_COMMAND)
    backup = CLAUDE_CONFIG.with_suffix(".json.bak")
    shutil.copy2(CLAUDE_CONFIG, backup)
    CLAUDE_CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[mcp] registered smriti server (backup: {backup})")


def _wake_command(memory_root: Path) -> str:
    return make_wake_hook_command(
        memory_root,
        home=HOME,
        framing=WAKE_FRAMING,
        audience="coding",
        style=WAKE_STYLE,
    )


def patch_settings_json(memory_root: Path) -> None:
    """Wire SessionStart / UserPromptSubmit / PostToolUse hooks.

    SessionStart wiring goes through the canonical hook model:
    an EQUIVALENT wake hook (any command form) is left untouched, a
    DEFICIENT one (right wake.py, wrong semantics) is upgraded in
    place, UNRELATED hooks are never modified.
    """
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
    wake_cmd = _wake_command(memory_root)
    activity_cmd = (
        f'touch "$HOME/{memory_rel}/.smriti/last-activity" 2>/dev/null || true'
    )
    recall_cmd = "python ~/.claude/hooks/associative_recall.py"

    changed = False

    def classify(cmd: str) -> str:
        verdict, _ = classify_wake_hook(
            cmd, memory_root=memory_root, home=HOME, framing=WAKE_FRAMING
        )
        return verdict

    # SessionStart: wake.py via the canonical hook model.
    session_start = hooks.setdefault("SessionStart", [])
    equivalent_found = False
    deficient_upgraded = False
    for group in session_start:
        for hook in group.get("hooks", []):
            verdict = classify(hook.get("command", ""))
            if verdict == EQUIVALENT:
                equivalent_found = True
            elif verdict == DEFICIENT:
                hook["command"] = wake_cmd
                deficient_upgraded = True

    if equivalent_found:
        print("[settings] SessionStart wake hook already wired (equivalent)")
    elif deficient_upgraded:
        print("[settings] SessionStart wake hook upgraded to canonical form")
        changed = True
    else:
        session_start.append(
            {"matcher": "", "hooks": [{"type": "command", "command": wake_cmd}]}
        )
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
    """Deploy hook shims into ``~/.claude/hooks/``."""
    deploy_hook_scripts(SHARED_HOOKS_DIR, HOOKS_DST, HOOK_DEPLOY_MAP)
    deploy_hook_scripts(HOOKS_SRC, HOOKS_DST, LOCAL_HOOKS)


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


def _claude_md_block(memory_root: Path) -> str:
    # The block carries no H1 — the title line belongs to the user's
    # document (kept on migration, supplied via ``title=`` on creation).
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    return compose_agent_doc(
        addendum=CLAUDE_CODE_ADDENDUM,
        memory_rel=memory_rel,
        header="_Generated by smriti — edit outside the markers only._",
    )


def preflight_agent_doc() -> str | None:
    """Validate ~/.claude/CLAUDE.md BEFORE any harness mutation.

    Returns None when safe to proceed, or a human-readable refusal.
    """
    state = classify_doc(CLAUDE_MD)
    if state.kind in ("missing", "managed"):
        return None
    return state.detail


def write_claude_md(memory_root: Path) -> None:
    if write_managed_doc(
        CLAUDE_MD,
        _claude_md_block(memory_root),
        title="# CLAUDE.md (user-global)",
    ):
        print(f"[CLAUDE.md] wrote managed block in {CLAUDE_MD}")
    else:
        print(f"[CLAUDE.md] {CLAUDE_MD} up to date")


def migrate_claude_md(memory_root: Path) -> None:
    """One-time legacy conversion. Raises ManagedDocError when unsafe."""
    backup = migrate_agent_doc(CLAUDE_MD, _claude_md_block(memory_root))
    print(f"[CLAUDE.md] migrated to managed block (backup: {backup})")


def run_claude_code(
    memory_root: Path,
    *,
    skip_settings: bool = False,
    skip_mcp: bool = False,
    migrate_doc: bool = False,
) -> bool:
    """Run all Claude-Code-specific install steps. Idempotent.

    Returns False (after mutating NOTHING) when the agent-doc preflight
    refuses — the dispatcher turns that into a nonzero exit so a
    partial install can never masquerade as success.
    """
    if migrate_doc and classify_doc(CLAUDE_MD).kind == "migration-required":
        try:
            migrate_claude_md(memory_root)
        except ManagedDocError as exc:
            print(f"[claude-code] migration failed: {exc}")
            return False

    refusal = preflight_agent_doc()
    if refusal is not None:
        print(f"[claude-code] REFUSED before any change: {refusal}")
        return False

    install_hook_scripts()
    if not skip_mcp:
        register_mcp_server()
    if not skip_settings:
        patch_settings_json(memory_root)
    write_claude_md(memory_root)
    return True

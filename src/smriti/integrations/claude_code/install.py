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

The two JSON files are Claude Code's own, rewritten while it runs; each edit
goes through :func:`smriti.integrations.common.config_file.edit_config`
(locked, re-read before writing, backed up, atomic). Paths are worked out
from the home directory when a step runs, not when this module is imported.

All operations are idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from smriti.integrations.common import (
    DEFICIENT,
    EQUIVALENT,
    SHARED_HOOKS_DIR,
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
from smriti.integrations.common.config_file import edit_config
from smriti.integrations.common.managed_doc import DocState
from smriti.integrations.common.mcp_spec import registered

HOOKS_SRC = Path(__file__).resolve().parent / "hooks"

# Claude Code runs hook commands under a POSIX shell on every platform
# (unlike Codex, which uses PowerShell on Windows — see codex/install.py).
WAKE_STYLE = "sh"
WAKE_FRAMING = "raw"

# One shared recall shim, deployed under this harness's local filename.
HOOK_DEPLOY_MAP = {"recall_shim.py": "associative_recall.py"}
# Claude-Code-only hooks that stay in this package.
LOCAL_HOOKS = ["precompact_capture.py"]


def _home(home: Path | None = None) -> Path:
    """*home* when given, else the user's home as the environment says now."""
    return home if home is not None else Path.home()


def claude_dir(home: Path | None = None) -> Path:
    return _home(home) / ".claude"


def settings_path(home: Path | None = None) -> Path:
    """``~/.claude/settings.json``: the hooks."""
    return claude_dir(home) / "settings.json"


def claude_md_path(home: Path | None = None) -> Path:
    return claude_dir(home) / "CLAUDE.md"


def claude_config_path(home: Path | None = None) -> Path:
    """``~/.claude.json``: Claude Code's own state, and the user-scope MCP servers."""
    return _home(home) / ".claude.json"


def hooks_dir(home: Path | None = None) -> Path:
    return claude_dir(home) / "hooks"


def _edit_json(path: Path, tag: str,
               change: Callable[[dict[str, Any], list[str]], bool]) -> bool:
    """Apply *change* to the JSON object in *path* (:func:`edit_config`); print what it did.

    *change* edits the object and says whether it changed it, noting what it
    did in the list it is given. False when a change couldn't be saved.
    """
    notes: list[str] = []

    def rewrite(text: str | None) -> str | None:
        notes.clear()  # a second pass, on the text Claude Code left, reports afresh
        if text is None:
            notes.append(f"{path} not found — skipping")
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            notes.append(f"parse error on {path}: {exc}; leaving untouched")
            return None
        if not isinstance(data, dict):
            notes.append(f"{path} doesn't hold a JSON object; leaving untouched")
            return None
        return json.dumps(data, indent=2) if change(data, notes) else None

    done = edit_config(path, rewrite)
    for note in notes:
        print(f"[{tag}] {note}")
    if done.refused:
        print(f"[{tag}] NOT SAVED: {done.refused}")
        return False
    if done.changed:
        print(f"[{tag}] saved {path}" + (f" (backup: {done.backup})" if done.backup else ""))
    return True


def register_mcp_server(memory_root: Path | None = None, *,
                        home: Path | None = None) -> bool:
    """Add the smriti MCP server to ``~/.claude.json`` at user scope.

    With ``memory_root``, the entry carries it (``env.SMRITI_ROOT``), so the
    server uses that root whatever environment Claude Code starts it in; an
    entry without it, or naming another root, is brought up to date, its
    other keys and variables kept. False when the change couldn't be saved
    (Claude Code kept rewriting the file).
    """
    config = claude_config_path(home)
    if not config.exists():
        print(f"[mcp] {config} not found — skipping (Claude Code not run yet?)")
        return True

    def register(data: dict[str, Any], notes: list[str]) -> bool:
        servers = data.setdefault("mcpServers", {})
        if mcp_registration_matches(servers.get("smriti"), memory_root):
            notes.append("smriti server already registered")
            return False
        servers["smriti"] = registered(servers.get("smriti"), memory_root)
        notes.append("registered smriti server")
        return True

    return _edit_json(config, "mcp", register)


def _wake_command(memory_root: Path, home: Path) -> str:
    return make_wake_hook_command(
        memory_root,
        home=home,
        framing=WAKE_FRAMING,
        audience="coding",
        style=WAKE_STYLE,
    )


def patch_settings_json(memory_root: Path, home: Path | None = None) -> bool:
    """Wire SessionStart / UserPromptSubmit / PostToolUse hooks.

    SessionStart wiring goes through the canonical hook model:
    an EQUIVALENT wake hook (any command form) is left untouched, a
    DEFICIENT one (right wake.py, wrong semantics) is upgraded in
    place, UNRELATED hooks are never modified. False when the change
    couldn't be saved (Claude Code kept rewriting the file).
    """
    home = _home(home)
    settings = settings_path(home)
    if not settings.exists():
        print(f"[settings] {settings} not found — skipping")
        return True
    return _edit_json(settings, "settings",
                      lambda data, notes: _wire_hooks(data, notes, memory_root, home))


def _wire_hooks(data: dict[str, Any], notes: list[str], memory_root: Path, home: Path) -> bool:
    """Wire smriti's hooks into settings.json's object; whether anything changed."""
    hooks = data.setdefault("hooks", {})
    memory_rel = memory_root.relative_to(home).as_posix()
    wake_cmd = _wake_command(memory_root, home)
    activity_cmd = (
        f'touch "$HOME/{memory_rel}/.smriti/last-activity" 2>/dev/null || true'
    )
    recall_cmd = "python ~/.claude/hooks/associative_recall.py"

    changed = False

    def classify(cmd: str) -> str:
        verdict, _ = classify_wake_hook(
            cmd, memory_root=memory_root, home=home, framing=WAKE_FRAMING
        )
        return verdict

    # SessionStart: wake.py via the canonical hook model. Exactly ONE
    # active smriti wake hook survives: the first equivalent one wins,
    # else the first deficient one is upgraded in place; every other
    # smriti wake hook (stale legacy, duplicate) is removed — two
    # active hooks would run the briefing twice. Unrelated hooks are
    # never modified.
    session_start = hooks.setdefault("SessionStart", [])
    equivalent_exists = any(
        classify(hook.get("command", "")) == EQUIVALENT
        for group in session_start
        for hook in group.get("hooks", [])
    )
    wake_kept = False
    deficient_upgraded = False
    duplicates_removed = False
    rebuilt_groups = []
    for group in session_start:
        kept_hooks = []
        for hook in group.get("hooks", []):
            verdict = classify(hook.get("command", ""))
            if verdict == EQUIVALENT and not wake_kept:
                wake_kept = True
                kept_hooks.append(hook)
            elif verdict == DEFICIENT and not equivalent_exists and not wake_kept:
                hook["command"] = wake_cmd
                wake_kept = True
                deficient_upgraded = True
                kept_hooks.append(hook)
            elif verdict in (EQUIVALENT, DEFICIENT):
                duplicates_removed = True  # ours, redundant — drop
            else:
                kept_hooks.append(hook)
        if kept_hooks:
            rebuilt_groups.append({**group, "hooks": kept_hooks})

    if not wake_kept:
        rebuilt_groups.append(
            {"matcher": "", "hooks": [{"type": "command", "command": wake_cmd}]}
        )
        notes.append("SessionStart -> wake.py")
    elif deficient_upgraded:
        notes.append("SessionStart wake hook upgraded to canonical form")
    else:
        notes.append("SessionStart wake hook already wired (equivalent)")
    if duplicates_removed:
        notes.append("removed redundant smriti wake hook(s)")
    if session_start != rebuilt_groups:
        hooks["SessionStart"] = rebuilt_groups
        changed = True

    # UserPromptSubmit: touch last-activity (additive)
    ups = hooks.setdefault("UserPromptSubmit", [])
    activity_wired = any(
        any(h.get("command") == activity_cmd for h in group.get("hooks", []))
        for group in ups
    )
    if activity_wired:
        notes.append("UserPromptSubmit activity hook already wired")
    else:
        if ups and ups[0].get("matcher", "") == "":
            ups[0].setdefault("hooks", []).append(
                {"type": "command", "command": activity_cmd}
            )
        else:
            ups.append(
                {"matcher": "", "hooks": [{"type": "command", "command": activity_cmd}]}
            )
        notes.append("UserPromptSubmit -> touch last-activity")
        changed = True

    # PostToolUse: associative recall on Read|Edit|Write
    post = hooks.setdefault("PostToolUse", [])
    recall_wired = any(
        any(h.get("command") == recall_cmd for h in group.get("hooks", []))
        for group in post
    )
    if recall_wired:
        notes.append("PostToolUse recall hook already wired")
    else:
        post.append({
            "matcher": "Read|Edit|Write",
            "hooks": [{"type": "command", "command": recall_cmd}],
        })
        notes.append("PostToolUse -> associative_recall.py")
        changed = True

    return changed


def install_hook_scripts(home: Path | None = None) -> None:
    """Deploy hook shims into ``~/.claude/hooks/``."""
    deploy_hook_scripts(SHARED_HOOKS_DIR, hooks_dir(home), HOOK_DEPLOY_MAP)
    deploy_hook_scripts(HOOKS_SRC, hooks_dir(home), LOCAL_HOOKS)


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


def _claude_md_block(memory_root: Path, home: Path | None = None) -> str:
    # The block carries no H1 — the title line belongs to the user's
    # document (kept on migration, supplied via ``title=`` on creation).
    memory_rel = f"~/{memory_root.relative_to(_home(home)).as_posix()}"
    return compose_agent_doc(
        addendum=CLAUDE_CODE_ADDENDUM,
        memory_rel=memory_rel,
        header="_Generated by smriti — edit outside the markers only._",
    )


def agent_doc_state(home: Path | None = None) -> DocState:
    """Classify ~/.claude/CLAUDE.md without modifying it."""
    return classify_doc(claude_md_path(home))


def preflight_agent_doc(home: Path | None = None) -> str | None:
    """Validate ~/.claude/CLAUDE.md BEFORE any harness mutation.

    Returns None when safe to proceed, or a human-readable refusal.
    """
    state = agent_doc_state(home)
    if state.kind in ("missing", "managed"):
        return None
    return state.detail


def write_claude_md(memory_root: Path, home: Path | None = None) -> None:
    claude_md = claude_md_path(home)
    if write_managed_doc(
        claude_md,
        _claude_md_block(memory_root, home),
        title="# CLAUDE.md (user-global)",
    ):
        print(f"[CLAUDE.md] wrote managed block in {claude_md}")
    else:
        print(f"[CLAUDE.md] {claude_md} up to date")


def migrate_claude_md(memory_root: Path, home: Path | None = None) -> None:
    """One-time legacy conversion. Raises ManagedDocError when unsafe."""
    backup = migrate_agent_doc(claude_md_path(home), _claude_md_block(memory_root, home))
    print(f"[CLAUDE.md] migrated to managed block (backup: {backup})")


def run_claude_code(
    memory_root: Path,
    *,
    home: Path | None = None,
    skip_settings: bool = False,
    skip_mcp: bool = False,
    migrate_doc: bool = False,
) -> bool:
    """Run all Claude-Code-specific install steps. Idempotent.

    Returns False (after mutating NOTHING) when the agent-doc preflight
    refuses — the dispatcher turns that into a nonzero exit so a
    partial install can never masquerade as success. Returns False too
    when a config file couldn't be saved (Claude Code kept rewriting
    it); that file is left as it was, and the install can be run again.
    """
    home = _home(home)
    if migrate_doc and classify_doc(claude_md_path(home)).kind == "migration-required":
        try:
            migrate_claude_md(memory_root, home)
        except ManagedDocError as exc:
            print(f"[claude-code] migration failed: {exc}")
            return False

    refusal = preflight_agent_doc(home)
    if refusal is not None:
        print(f"[claude-code] REFUSED before any change: {refusal}")
        return False

    install_hook_scripts(home)
    saved = True
    if not skip_mcp:
        saved = register_mcp_server(memory_root, home=home) and saved
    if not skip_settings:
        saved = patch_settings_json(memory_root, home) and saved
    write_claude_md(memory_root, home)
    return saved

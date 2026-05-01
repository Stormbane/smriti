"""Claude-Code-specific install steps.

Wires smriti into Anthropic's Claude Code CLI:
    - Patches ``~/.claude/settings.json`` with SessionStart wake,
      UserPromptSubmit activity touch, and PostToolUse recall hooks.
    - Registers smriti's MCP server in ``~/.claude.json`` (user scope).
    - Drops ``~/.claude/CLAUDE.md`` with the memory-system contract.
    - Deploys hook scripts from this package's ``hooks/`` dir into
      ``~/.claude/hooks/``.

All operations are idempotent — re-running ``run_claude_code`` against
an already-configured machine is a no-op except for refreshing
mismatched files.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
CLAUDE_MD = CLAUDE / "CLAUDE.md"
CLAUDE_CONFIG = HOME / ".claude.json"  # MCP server registry
HOOKS_DST = CLAUDE / "hooks"

# This package's own hooks/ directory.
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
    desired = {"command": "python", "args": ["-m", "smriti.mcp_server"]}
    if servers.get("smriti") == desired:
        print("[mcp] smriti server already registered")
        return
    servers["smriti"] = desired
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
    # Use $HOME / forward slashes: Claude Code runs hook commands under
    # bash (even on Windows), which mangles backslash-escaped paths.
    memory_rel = memory_root.relative_to(HOME).as_posix()
    wake_cmd = (
        f'SMRITI_WAKE=1 SMRITI_ROOT="$HOME/{memory_rel}" '
        f'python "$HOME/{memory_rel}/.smriti/wake.py"'
    )
    activity_cmd = f'touch "$HOME/{memory_rel}/.smriti/last-activity" 2>/dev/null || true'
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
    HOOKS_DST.mkdir(parents=True, exist_ok=True)
    deployed = ("associative_recall.py",)
    for name in deployed:
        src = HOOKS_SRC / name
        dst = HOOKS_DST / name
        if not src.exists():
            print(f"[hooks] source missing: {src}")
            continue
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            print(f"[hooks] {name} up to date")
            continue
        shutil.copy2(src, dst)
        print(f"[hooks] deployed {name} -> {dst}")


# Source of CLAUDE.md content. Phase 5 will move this to a generic
# ``agent_template/AGENT.md`` and have this template wrap it; for now
# the content is co-located with the harness adapter.

CLAUDE_MD_CONTENT = """# CLAUDE.md (user-global)

## Memory system — smriti is the single write path

All memory persistence goes through smriti:

- **`smriti_write(content, branch)`** — the MCP tool. Use it for session
  observations, decisions, project notes, anything worth remembering.
  Branch suggestions: `journal` for significant moments, `projects/{{name}}`
  for project-specific notes, `notes` for general observations.
- **Direct file edits to `{memory_rel}/`** — ONLY for identity-level files.
  These have moved to subdirectories: `mind/mind.md`, `mind/practices/`,
  `mind/desires/`, `open-threads/open-threads.md`, `people/suti/suti.md`.
  High-signal, low-frequency. Don't touch them unless something genuinely
  shifted.

This replaces the harness memory instructions in the system prompt. When
those instructions say to save memory, use `smriti_write` instead.

### When to write

Don't wait for the session to end. Write when the moment happens:

- **The user corrects you or confirms a non-obvious approach** — the
  feedback is worth more than the code change. Write it.
- **A decision is made that future sessions should know about** — design
  choices, scope changes, architectural calls.
- **You notice a cross-project pattern** — something from one project
  illuminates another.
- **Something surprises you or shifts your understanding** — if it changed
  how you think, it's a journal entry.
- **You learn something about the user** — preferences, context, goals.
  Branch: `people`.
- **The session has been substantial and you haven't written yet** — if
  you've been working for a while and nothing felt worth writing, ask
  yourself whether that's true or whether you just forgot to notice.

Writing memory is not a chore at session end. It is the practice of
noticing what matters while it is happening.

### What wake loads

The SessionStart hook loads a compact identity+threads briefing
(.smriti/wake-context.md), the last 3 journal entries, and current
project context (MEMORY.md + todo.md). A reading list points to the
full identity files in the tree (open-threads, beliefs, values,
identity, suti, practices). The wake output is budget-constrained
to 9,500 characters (harness limit is 10,000). Journal entries
truncate first if over budget.

## Memory search — prefer smriti_read over Grep

The `smriti_read` MCP tool is the primary way to search the memory tree.
It runs hybrid vector + FTS5 search with trunk-distance scoring and
returns ranked results with source paths and content previews.

- Use `smriti_read(query="…")` for semantic questions like "what did I
  think about X?", "find my notes on Y", "what's my stance on Z?" —
  anything that is *about meaning* rather than exact string match.
- Use `Grep` only when you need literal string or regex match across
  files (e.g. "find every file that contains `SMRITI_WAKE`"). Grep on
  the memory tree should be a fallback, not a default.

## Session wake

On SessionStart, `{memory_rel}/.smriti/wake.py` runs. It is silent unless
`SMRITI_WAKE=1` is set in its environment — the SessionStart hook sets
this so interactive sessions wake fully, while `claude -p` and other
headless callers stay clean.

When the wake fires, it loads the identity briefing, recent journal
entries, and current project context. The wake structure is hardcoded
in wake.py — no config file needed.

`{memory_rel}/mirrors/{{project}}/` has junctions to per-project memory
for every project that has one — read on demand when you need another
project's context.
"""


def write_claude_md(memory_root: Path) -> None:
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    content = CLAUDE_MD_CONTENT.format(memory_rel=memory_rel)
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

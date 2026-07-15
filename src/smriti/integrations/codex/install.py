"""Codex-CLI-specific install steps.

Wires smriti into OpenAI's Codex CLI:
    - Patches ``~/.codex/config.toml``:
        * ``[features] hooks = true`` (enables the hooks system)
        * ``[[hooks.SessionStart]]`` -> wake.py with
          ``SMRITI_WAKE_FRAMING=codex-json`` so identity is force-loaded
          before the first user turn (this is the equivalent of Claude
          Code's CLAUDE.md + SessionStart combo)
        * ``[mcp_servers.smriti]`` for explicit smriti_read/smriti_write
    - Drops ``~/.codex/AGENTS.md`` (Codex auto-loads this on every run;
      32 KiB cap, root-first concatenation across project chain). Body
      composed from the shared ``templates/AGENT.md`` plus a Codex-
      specific addendum.

Force-injection mechanism: the SessionStart hook runs under Codex's
own process before the first user turn. Its stdout (JSON
``hookSpecificOutput.additionalContext``) is appended to the developer
context. The agent has no opportunity to skip it — identity is loaded
at the protocol level, not by request.

Codex hook docs reference:
    https://developers.openai.com/codex/hooks
    https://developers.openai.com/codex/config-reference

This module shares MCP spec, AGENT.md composition, hook deployment,
and wake-command construction with ``claude_code/install.py`` via
``smriti.integrations.common``.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

from smriti.integrations.common import (
    SMRITI_MCP_COMMAND,
    compose_agent_doc,
    deploy_hook_scripts,
    make_wake_hook_command,
)

HOME = Path.home()
CODEX = HOME / ".codex"
CONFIG_TOML = CODEX / "config.toml"
AGENTS_MD = CODEX / "AGENTS.md"
HOOKS_DST = CODEX / "hooks"
HOOKS_SRC = Path(__file__).resolve().parent / "hooks"


# Codex-specific addendum appended after the generic AGENT.md body.
# Speaks to the SessionStart hook, AGENTS.md precedence chain, and the
# 32 KiB cap — things only this harness has.
CODEX_ADDENDUM = """## Session wake (Codex CLI)

On SessionStart, `{memory_rel}/.smriti/wake.py` runs via the hook
configured in `~/.codex/config.toml` under `[[hooks.SessionStart]]`.
The hook emits `hookSpecificOutput.additionalContext` JSON, which
Codex appends to the developer context before the first user turn.
This means identity is loaded at the protocol level — there is no
request the agent can choose to skip.

The wake script is silent unless `SMRITI_WAKE=1` is set in its
environment (the hook command sets it). `SMRITI_WAKE_FRAMING=codex-json`
selects the Codex-shaped output (vs. Claude Code's raw stdout).

## AGENTS.md precedence

Codex auto-loads AGENTS.md from a precedence chain on every run:

1. `$CODEX_HOME/AGENTS.override.md` (else `$CODEX_HOME/AGENTS.md`)
2. Project chain from git root down to cwd: `AGENTS.override.md` ->
   `AGENTS.md` -> any names listed in `project_doc_fallback_filenames`.

Files concatenate root-first; total cap is 32 KiB
(`project_doc_max_bytes`). This file (`~/.codex/AGENTS.md`) holds the
user-global memory contract; project-level AGENTS.md files override.

## Memory search — ambient recall is wired

PostToolUse recall is wired on `Edit|Write|apply_patch` via
`~/.codex/config.toml`. After every patch, smriti runs recall against
the touched files and injects relevant memory as
`hookSpecificOutput.additionalContext`. Bash and MCP tools are
intentionally skipped (Bash is too noisy; MCP would recurse on
`smriti_read`).

For pure-text turns where no patch fires (planning, discussion), the
"When to call `smriti_read`" guidance above is load-bearing — call it
explicitly when topics open, when you encounter unfamiliar
references, and when starting substantive turns without recent file
context.
"""


def _import_tomli_w() -> object:
    try:
        import tomli_w  # type: ignore[import-untyped]
    except ImportError:
        print(
            "[codex] tomli-w not installed — required to write config.toml.\n"
            "        pip install -e '.[codex]'   # or: pip install tomli-w",
            file=sys.stderr,
        )
        sys.exit(1)
    return tomli_w


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"[codex] parse error on {path}: {exc}; aborting")
        sys.exit(1)


def _write_toml(path: Path, data: dict) -> None:
    tomli_w = _import_tomli_w()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def patch_config_toml(memory_root: Path) -> None:
    """Idempotently set hook + MCP server entries in ``~/.codex/config.toml``.

    Steps:
        1. ``[features] codex_hooks = true``
        2. ``[[hooks.SessionStart]]`` with our wake command
        3. ``[mcp_servers.smriti]`` pointing at the MCP entrypoint
    """
    data = _read_toml(CONFIG_TOML)
    changed = False

    # 1. Enable hooks. ``codex_hooks`` remains a compatibility alias but
    # ``hooks`` is the current documented key.
    features = data.setdefault("features", {})
    if features.get("hooks") is not True:
        features["hooks"] = True
        print("[codex] [features] hooks = true")
        changed = True

    # 2. SessionStart hook -> wake.py with codex-json framing.
    wake_cmd = make_wake_hook_command(
        memory_root, home=HOME, framing="codex-json", audience="coding"
    )
    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    # Codex's hook shape is:
    #   [[hooks.SessionStart]]
    #     matcher = "startup|resume"
    #     [[hooks.SessionStart.hooks]]
    #     type = "command"
    #     command = "..."
    def is_smriti_wake(hook: dict) -> bool:
        command = hook.get("command", "")
        return "SMRITI_ROOT=" in command and "/.smriti/wake.py" in command

    # Replace only Smriti-managed legacy hooks. User hooks, including hooks
    # in a group that also contained the legacy wake hook, survive intact.
    retained_groups = []
    managed_found = False
    for group in session_start:
        retained_hooks = [hook for hook in group.get("hooks", []) if not is_smriti_wake(hook)]
        if len(retained_hooks) != len(group.get("hooks", [])):
            managed_found = True
        if retained_hooks:
            retained_groups.append({**group, "hooks": retained_hooks})

    canonical_group = {
        "matcher": "startup|resume|clear|compact",
        "hooks": [{
            "type": "command",
            "command": wake_cmd,
            "statusMessage": "Loading smriti memory briefing",
        }],
    }
    if managed_found:
        replacement = [*retained_groups, canonical_group]
        if session_start != replacement:
            hooks["SessionStart"] = replacement
            print("[codex] [[hooks.SessionStart]] -> wake.py (migrated)")
            changed = True
        else:
            print("[codex] SessionStart wake hook already wired")
    else:
        hooks["SessionStart"].append(canonical_group)
        print("[codex] [[hooks.SessionStart]] -> wake.py (codex-json framing)")
        changed = True

    # 3. PostToolUse hook -> recall_hook.py (codex-json framing).
    # Codex's matcher accepts apply_patch's aliases (Edit, Write) so a
    # Claude-Code-style matcher works even though Codex's actual edit
    # tool is apply_patch. The recall hook handles all three names.
    recall_cmd = (
        'SMRITI_RECALL_FRAMING="codex-json" '
        'python "$HOME/.codex/hooks/recall_hook.py"'
    )
    post_tool = hooks.setdefault("PostToolUse", [])
    recall_wired = False
    for group in post_tool:
        for h in group.get("hooks", []):
            if h.get("command") == recall_cmd:
                recall_wired = True
                break
        if recall_wired:
            break
    if recall_wired:
        print("[codex] PostToolUse recall hook already wired")
    else:
        post_tool.append({
            "matcher": "Edit|Write|apply_patch",
            "hooks": [{
                "type": "command",
                "command": recall_cmd,
                "statusMessage": "Smriti recall",
                "timeout": 10,
            }],
        })
        print("[codex] [[hooks.PostToolUse]] -> recall_hook.py (apply_patch)")
        changed = True

    # 4. MCP server registration
    mcp = data.setdefault("mcp_servers", {})
    desired = {
        "command": SMRITI_MCP_COMMAND["command"],
        "args": list(SMRITI_MCP_COMMAND["args"]),
    }
    if mcp.get("smriti") == desired:
        print("[codex] [mcp_servers.smriti] already registered")
    else:
        mcp["smriti"] = desired
        print("[codex] [mcp_servers.smriti] registered")
        changed = True

    if not changed:
        return

    if CONFIG_TOML.exists():
        backup = CONFIG_TOML.with_suffix(".toml.bak")
        shutil.copy2(CONFIG_TOML, backup)
        print(f"[codex] backup: {backup}")
    _write_toml(CONFIG_TOML, data)
    print(f"[codex] saved {CONFIG_TOML}")


def write_agents_md(memory_root: Path) -> None:
    """Drop ~/.codex/AGENTS.md composed from the shared template."""
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    content = compose_agent_doc(
        addendum=CODEX_ADDENDUM,
        memory_rel=memory_rel,
        header="# AGENTS.md (user-global) — smriti memory contract",
    )
    if len(content) > 32_000:
        print(
            f"[codex] WARNING: AGENTS.md is {len(content)} bytes; "
            "Codex caps project-doc loading at 32 KiB. Consider trimming."
        )
    CODEX.mkdir(parents=True, exist_ok=True)
    if AGENTS_MD.exists() and AGENTS_MD.read_text(encoding="utf-8") == content:
        print(f"[codex] {AGENTS_MD} up to date")
        return
    AGENTS_MD.write_text(content, encoding="utf-8")
    print(f"[codex] wrote {AGENTS_MD}")


def install_hook_scripts() -> None:
    """Deploy hooks from this package into ``~/.codex/hooks/``."""
    deploy_hook_scripts(HOOKS_SRC, HOOKS_DST, ["recall_hook.py"])


def run_codex(
    memory_root: Path,
    *,
    skip_config: bool = False,
) -> None:
    """Run all Codex-specific install steps. Idempotent."""
    install_hook_scripts()
    if not skip_config:
        patch_config_toml(memory_root)
    write_agents_md(memory_root)

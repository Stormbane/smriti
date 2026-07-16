"""Codex-CLI-specific install steps.

Wires smriti into OpenAI's Codex CLI:
    - Patches ``~/.codex/config.toml``:
        * ``[features] hooks = true`` (enables the hooks system)
        * ``[[hooks.SessionStart]]`` -> wake.py with
          ``SMRITI_WAKE_FRAMING=codex-json`` so identity is force-loaded
          before the first user turn (this is the equivalent of Claude
          Code's CLAUDE.md + SessionStart combo)
        * ``[mcp_servers.smriti]`` for explicit smriti_read/smriti_write
    - Maintains the smriti-managed block in ``~/.codex/AGENTS.md``
      (Codex auto-loads this on every run; 32 KiB cap, root-first
      concatenation across project chain). Unmarked legacy files are
      refused at preflight and migrated once via ``--migrate-agent-doc``.
    - Deploys the shared recall shim into ``~/.codex/hooks/``.

Platform note: Codex runs hook commands under PowerShell on Windows —
NOT bash — so the wake command is emitted as a PowerShell
``-EncodedCommand`` there. Claude Code, by contrast, runs hooks under a
POSIX shell on every platform (see claude_code/install.py). The
canonical hook model (``integrations.common.hook_model``) parses and
classifies both forms, so the installer never regresses a working hook
of either shape.

Force-injection mechanism: the SessionStart hook runs under Codex's
own process before the first user turn. Its stdout (JSON
``hookSpecificOutput.additionalContext``) is appended to the developer
context. The agent has no opportunity to skip it — identity is loaded
at the protocol level, not by request.

Codex hook docs reference:
    https://developers.openai.com/codex/hooks
    https://developers.openai.com/codex/config-reference

This module shares MCP spec, AGENT.md composition, managed-doc
machinery, hook deployment, and the wake-hook model with
``claude_code/install.py`` via ``smriti.integrations.common``.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
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
CODEX = HOME / ".codex"
CONFIG_TOML = CODEX / "config.toml"
AGENTS_MD = CODEX / "AGENTS.md"
HOOKS_DST = CODEX / "hooks"

WAKE_FRAMING = "codex-json"
SESSION_START_MATCHER = "startup|resume|clear|compact"

# One shared recall shim, deployed under this harness's local filename.
HOOK_DEPLOY_MAP = {"recall_shim.py": "recall_hook.py"}


def _wake_style() -> str:
    """Codex runs hooks under PowerShell on Windows, POSIX sh elsewhere."""
    return "powershell-encoded" if sys.platform == "win32" else "sh"


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
        1. ``[features] hooks = true``
        2. ``[[hooks.SessionStart]]`` with our wake command, via the
           canonical hook model: an EQUIVALENT hook in a group with the
           right matcher is left byte-for-byte untouched (never regress
           a working hook, whatever its command form); DEFICIENT hooks
           and equivalent hooks under a wrong matcher are migrated to
           the canonical group; UNRELATED hooks survive intact.
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
        memory_root,
        home=HOME,
        framing=WAKE_FRAMING,
        audience="coding",
        style=_wake_style(),
    )
    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    def classify(cmd: str) -> str:
        verdict, _ = classify_wake_hook(
            cmd, memory_root=memory_root, home=HOME, framing=WAKE_FRAMING
        )
        return verdict

    # Codex's hook shape is:
    #   [[hooks.SessionStart]]
    #     matcher = "startup|resume|clear|compact"
    #     [[hooks.SessionStart.hooks]]
    #     type = "command"
    #     command = "..."
    properly_wired = False
    needs_canonical = False
    retained_groups = []
    for group in session_start:
        matcher_ok = group.get("matcher") == SESSION_START_MATCHER
        retained_hooks = []
        for hook in group.get("hooks", []):
            verdict = classify(hook.get("command", ""))
            if verdict == EQUIVALENT and matcher_ok:
                properly_wired = True
                retained_hooks.append(hook)
            elif verdict in (EQUIVALENT, DEFICIENT):
                # Ours, but wrong semantics or wrong matcher — replace
                # with the canonical group below.
                needs_canonical = True
            else:
                retained_hooks.append(hook)
        if retained_hooks:
            retained_groups.append({**group, "hooks": retained_hooks})

    if properly_wired and not needs_canonical:
        print("[codex] SessionStart wake hook already wired (equivalent)")
    else:
        if not properly_wired:
            retained_groups.append({
                "matcher": SESSION_START_MATCHER,
                "hooks": [{
                    "type": "command",
                    "command": wake_cmd,
                    "statusMessage": "Loading smriti memory briefing",
                }],
            })
        if session_start != retained_groups:
            hooks["SessionStart"] = retained_groups
            print("[codex] [[hooks.SessionStart]] -> wake.py (canonical)")
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
    recall_wired = any(
        h.get("command") == recall_cmd
        for group in post_tool
        for h in group.get("hooks", [])
    )
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

    # 4. MCP server registration. Superset match: Codex annotates the
    # entry with tools.* approval-mode subtables; extras are fine.
    mcp = data.setdefault("mcp_servers", {})
    if mcp_registration_matches(mcp.get("smriti")):
        print("[codex] [mcp_servers.smriti] already registered")
    else:
        existing = mcp.get("smriti")
        desired = {
            "command": SMRITI_MCP_COMMAND["command"],
            "args": list(SMRITI_MCP_COMMAND["args"]),
        }
        if isinstance(existing, dict):
            # Preserve user annotations (approval modes etc.), fix the
            # command surface.
            existing.update(desired)
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


def _agents_md_block(memory_root: Path) -> str:
    # The block carries no H1 — the title line belongs to the document
    # (kept on migration, supplied via ``title=`` on creation).
    memory_rel = f"~/{memory_root.relative_to(HOME).as_posix()}"
    return compose_agent_doc(
        addendum=CODEX_ADDENDUM,
        memory_rel=memory_rel,
        header="_Generated by smriti — edit outside the markers only._",
    )


def preflight_agent_doc() -> str | None:
    """Validate ~/.codex/AGENTS.md BEFORE any harness mutation.

    Returns None when safe to proceed, or a human-readable refusal.
    """
    state = classify_doc(AGENTS_MD)
    if state.kind in ("missing", "managed"):
        return None
    return state.detail


def write_agents_md(memory_root: Path) -> None:
    """Create or rewrite the managed block in ~/.codex/AGENTS.md."""
    content = _agents_md_block(memory_root)
    if len(content) > 32_000:
        print(
            f"[codex] WARNING: AGENTS.md block is {len(content)} bytes; "
            "Codex caps project-doc loading at 32 KiB. Consider trimming."
        )
    if write_managed_doc(
        AGENTS_MD,
        content,
        title="# AGENTS.md (user-global) — smriti memory contract",
    ):
        print(f"[codex] wrote managed block in {AGENTS_MD}")
    else:
        print(f"[codex] {AGENTS_MD} up to date")


def migrate_agents_md(memory_root: Path) -> None:
    """One-time legacy conversion. Raises ManagedDocError when unsafe."""
    backup = migrate_agent_doc(AGENTS_MD, _agents_md_block(memory_root))
    print(f"[codex] migrated AGENTS.md to managed block (backup: {backup})")


def install_hook_scripts() -> None:
    """Deploy the shared recall shim into ``~/.codex/hooks/``."""
    deploy_hook_scripts(SHARED_HOOKS_DIR, HOOKS_DST, HOOK_DEPLOY_MAP)


def run_codex(
    memory_root: Path,
    *,
    skip_config: bool = False,
    migrate_doc: bool = False,
) -> bool:
    """Run all Codex-specific install steps. Idempotent.

    Returns False (after mutating NOTHING) when the agent-doc preflight
    refuses — the dispatcher turns that into a nonzero exit so a
    partial install can never masquerade as success.
    """
    if migrate_doc and classify_doc(AGENTS_MD).kind == "migration-required":
        try:
            migrate_agents_md(memory_root)
        except ManagedDocError as exc:
            print(f"[codex] migration failed: {exc}")
            return False

    refusal = preflight_agent_doc()
    if refusal is not None:
        print(f"[codex] REFUSED before any change: {refusal}")
        return False

    install_hook_scripts()
    if not skip_config:
        patch_config_toml(memory_root)
    write_agents_md(memory_root)
    return True

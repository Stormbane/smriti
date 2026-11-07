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
machinery, the wake-hook model and config-file editing
(:func:`smriti.integrations.common.config_file.edit_config`: locked,
re-read before writing, backed up, atomic) with ``claude_code/install.py``
via ``smriti.integrations.common``. Paths are worked out from the home
directory when a step runs, not when this module is imported.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

from smriti.integrations.common import (
    DEFICIENT,
    EQUIVALENT,
    ManagedDocError,
    classify_doc,
    classify_wake_hook,
    compose_agent_doc,
    make_wake_hook_command,
    mcp_registration_matches,
    migrate_agent_doc,
    write_managed_doc,
)
from smriti.integrations.common.config_file import edit_config
from smriti.integrations.common.managed_doc import DocState
from smriti.integrations.common.mcp_spec import registered

WAKE_FRAMING = "codex-json"
SESSION_START_MATCHER = "startup|resume|clear|compact"


def _home(home: Path | None = None) -> Path:
    """*home* when given, else the user's home as the environment says now."""
    return home if home is not None else Path.home()


def codex_dir(home: Path | None = None) -> Path:
    return _home(home) / ".codex"


def config_toml_path(home: Path | None = None) -> Path:
    return codex_dir(home) / "config.toml"


def agents_md_path(home: Path | None = None) -> Path:
    return codex_dir(home) / "AGENTS.md"


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
"""


def _import_tomli_w() -> ModuleType:
    try:
        import tomli_w
    except ImportError:
        print(
            "[codex] tomli-w not installed — required to write config.toml.\n"
            "        pip install -e '.[codex]'   # or: pip install tomli-w",
            file=sys.stderr,
        )
        sys.exit(1)
    return tomli_w


def _parse_toml(path: Path, text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        print(f"[codex] parse error on {path}: {exc}; aborting")
        sys.exit(1)


def patch_config_toml(memory_root: Path, home: Path | None = None) -> bool:
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

    False when the change couldn't be saved (the file kept changing).
    """
    home = _home(home)
    config = config_toml_path(home)
    notes: list[str] = []

    def rewrite(text: str | None) -> str | None:
        notes.clear()  # a second pass, on the text Codex left, reports afresh
        data = _parse_toml(config, text)
        if not _wire_config(data, notes, memory_root, home):
            return None
        tomli_w = _import_tomli_w()  # only when there is something to write
        text_out: str = tomli_w.dumps(data)
        return text_out

    done = edit_config(config, rewrite)
    for note in notes:
        print(f"[codex] {note}")
    if done.refused:
        print(f"[codex] NOT SAVED: {done.refused}")
        return False
    if done.changed:
        if done.backup is not None:
            print(f"[codex] backup: {done.backup}")
        print(f"[codex] saved {config}")
    return True


def _wire_config(data: dict[str, Any], notes: list[str], memory_root: Path,
                 home: Path) -> bool:
    """Wire smriti's hooks and MCP server into config.toml's data; whether anything changed."""
    changed = False

    # 1. Enable hooks. ``codex_hooks`` remains a compatibility alias but
    # ``hooks`` is the current documented key.
    features = data.setdefault("features", {})
    if features.get("hooks") is not True:
        features["hooks"] = True
        notes.append("[features] hooks = true")
        changed = True

    # 2. SessionStart hook -> wake.py with codex-json framing.
    wake_cmd = make_wake_hook_command(
        memory_root,
        home=home,
        framing=WAKE_FRAMING,
        audience="coding",
        style=_wake_style(),
    )
    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    def classify(cmd: str) -> str:
        verdict, _ = classify_wake_hook(
            cmd, memory_root=memory_root, home=home, framing=WAKE_FRAMING
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
            if verdict == EQUIVALENT and matcher_ok and not properly_wired:
                # First properly-wired hook wins; further smriti wake
                # hooks are redundant and would run the briefing twice.
                properly_wired = True
                retained_hooks.append(hook)
            elif verdict in (EQUIVALENT, DEFICIENT):
                # Ours, but redundant, wrong semantics, or wrong
                # matcher — subsumed by the canonical group below.
                needs_canonical = True
            else:
                retained_hooks.append(hook)
        if retained_hooks:
            retained_groups.append({**group, "hooks": retained_hooks})

    if properly_wired and not needs_canonical:
        notes.append("SessionStart wake hook already wired (equivalent)")
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
            notes.append("[[hooks.SessionStart]] -> wake.py (canonical)")
            changed = True

    # 3. MCP server registration, with the memory root in its env. Superset
    # match: Codex annotates the entry with tools.* approval-mode subtables;
    # extras are fine.
    mcp = data.setdefault("mcp_servers", {})
    if mcp_registration_matches(mcp.get("smriti"), memory_root):
        notes.append("[mcp_servers.smriti] already registered")
    else:
        # The user's annotations (approval modes etc.) and other variables
        # stay; the command surface and the root are fixed.
        mcp["smriti"] = registered(mcp.get("smriti"), memory_root)
        notes.append("[mcp_servers.smriti] registered")
        changed = True

    return changed


def _agents_md_block(memory_root: Path, home: Path | None = None) -> str:
    # The block carries no ``#`` title — the title line belongs to the document
    # (kept on migration, supplied via ``title=`` on creation).
    memory_rel = f"~/{memory_root.relative_to(_home(home)).as_posix()}"
    return compose_agent_doc(
        addendum=CODEX_ADDENDUM,
        memory_rel=memory_rel,
        header="_Generated by smriti — edit outside the markers only._",
    )


def agent_doc_state(home: Path | None = None) -> DocState:
    """Classify ~/.codex/AGENTS.md without modifying it."""
    return classify_doc(agents_md_path(home))


def preflight_agent_doc(home: Path | None = None) -> str | None:
    """Validate ~/.codex/AGENTS.md BEFORE any harness mutation.

    Returns None when safe to proceed, or a human-readable refusal.
    """
    state = agent_doc_state(home)
    if state.kind in ("missing", "managed"):
        return None
    return state.detail


def write_agents_md(memory_root: Path, home: Path | None = None) -> None:
    """Create or rewrite the managed block in ~/.codex/AGENTS.md."""
    agents_md = agents_md_path(home)
    content = _agents_md_block(memory_root, home)
    if len(content) > 32_000:
        print(
            f"[codex] WARNING: AGENTS.md block is {len(content)} bytes; "
            "Codex caps project-doc loading at 32 KiB. Consider trimming."
        )
    if write_managed_doc(
        agents_md,
        content,
        title="# AGENTS.md (user-global) — smriti memory contract",
    ):
        print(f"[codex] wrote managed block in {agents_md}")
    else:
        print(f"[codex] {agents_md} up to date")


def migrate_agents_md(memory_root: Path, home: Path | None = None) -> None:
    """One-time legacy conversion. Raises ManagedDocError when unsafe."""
    backup = migrate_agent_doc(agents_md_path(home), _agents_md_block(memory_root, home))
    print(f"[codex] migrated AGENTS.md to managed block (backup: {backup})")


def run_codex(
    memory_root: Path,
    *,
    home: Path | None = None,
    skip_config: bool = False,
    migrate_doc: bool = False,
) -> bool:
    """Run all Codex-specific install steps. Idempotent.

    Returns False (after mutating NOTHING) when the agent-doc preflight
    refuses — the dispatcher turns that into a nonzero exit so a
    partial install can never masquerade as success. Returns False too
    when config.toml couldn't be saved (it kept changing); it is left as
    it was, and the install can be run again.
    """
    home = _home(home)
    if migrate_doc and classify_doc(agents_md_path(home)).kind == "migration-required":
        try:
            migrate_agents_md(memory_root, home)
        except ManagedDocError as exc:
            print(f"[codex] migration failed: {exc}")
            return False

    refusal = preflight_agent_doc(home)
    if refusal is not None:
        print(f"[codex] REFUSED before any change: {refusal}")
        return False

    saved = patch_config_toml(memory_root, home) if not skip_config else True
    write_agents_md(memory_root, home)
    return saved

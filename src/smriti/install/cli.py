"""The installer: ``python -m smriti.install`` (``scripts/install.py`` in a checkout).

Runs the harness-neutral core install (memory tree, wake templates,
mirror junctions, qmd recall daemon), then dispatches to a per-harness
installer chosen via ``--harness``.

Defaults to ``--harness=claude_code`` for back-compat with prior
documentation. Pass ``--harness=none`` to install only the core (useful
when wiring smriti into a custom Python agent — see
``examples/python_agent.py``).

The actual install logic lives in:
    smriti/install/core.py                          (core)
    smriti/integrations/<harness>/install.py        (per-harness)

Run on a fresh machine after ``pip install smriti[read]`` (or ``pip
install -e .`` in a checkout)::

    python -m smriti.install
    python -m smriti.install --harness none
    python -m smriti.install --harness claude_code --skip-settings
    python -m smriti.install --no-capture --remote-is-private

Before changing anything it asks two privacy questions (capture
conversations into the day-log? is the backup remote private?), or takes
the answers from flags; see ``smriti/install/privacy.py``. Its last
lines say what is captured, what is committed and where it is pushed.

Re-runnable: idempotent, never deletes existing user files.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from smriti.install import privacy
from smriti.install.core import (
    DEFAULT_PROJECTS_ROOT,
    default_memory_root,
    run_core,
)


KNOWN_HARNESSES = ("claude_code", "codex", "hermes", "none")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m smriti.install")
    parser.add_argument(
        "--memory-root",
        default=None,
        help="Entity memory root (default: $SMRITI_ROOT, else ~/.narada if it exists, "
             "else ~/.smriti)",
    )
    parser.add_argument(
        "--projects-root",
        default=str(DEFAULT_PROJECTS_ROOT),
        help="Directory containing per-project source checkouts",
    )
    parser.add_argument(
        "--harness",
        default="claude_code",
        help=f"Agent harness to wire smriti into. Known: {', '.join(KNOWN_HARNESSES)}. "
             "Use 'none' for core-only install (e.g., custom Python agent).",
    )
    parser.add_argument(
        "--skip-settings",
        action="store_true",
        help="(harness=claude_code) Don't patch ~/.claude/settings.json",
    )
    parser.add_argument(
        "--skip-mcp",
        action="store_true",
        help="(harness=claude_code) Don't register the smriti MCP server",
    )
    parser.add_argument(
        "--skip-config",
        action="store_true",
        help="(harness=codex) Don't patch ~/.codex/config.toml",
    )
    parser.add_argument(
        "--skip-recall-daemon",
        action="store_true",
        help="Don't auto-start qmd's HTTP daemon for associative recall",
    )
    parser.add_argument(
        "--migrate-agent-doc",
        action="store_true",
        help="One-time conversion of a legacy (unmarked) agent doc "
             "(~/.claude/CLAUDE.md or ~/.codex/AGENTS.md) to the "
             "smriti:managed marker form, with a .pre-migrate.bak backup",
    )
    capture = parser.add_mutually_exclusive_group()
    capture.add_argument(
        "--capture",
        dest="capture",
        action="store_const",
        const=True,
        default=None,
        help="Capture conversations (Claude Code, Codex and voice transcripts) "
             "into <root>/log/, which the backup commits and pushes: set "
             '"capture": true in <root>/.smriti/daylog.json (capture is off until it is set)',
    )
    capture.add_argument(
        "--no-capture",
        dest="capture",
        action="store_const",
        const=False,
        help='Capture nothing: set "sources": [] in <root>/.smriti/daylog.json, '
             "keeping its other keys",
    )
    remote = parser.add_mutually_exclusive_group()
    remote.add_argument(
        "--remote-is-private",
        dest="remote_private",
        action="store_const",
        const=True,
        default=None,
        help="Say the memory root's backup remote (origin) is a private repository",
    )
    remote.add_argument(
        "--remote-not-private",
        dest="remote_private",
        action="store_const",
        const=False,
        help="Say origin is not private; the installer prints how to set up git-crypt",
    )
    args = parser.parse_args(argv)

    memory_root = (Path(args.memory_root).expanduser() if args.memory_root
                   else default_memory_root())
    projects_root = Path(args.projects_root).expanduser()
    harness = args.harness.strip().lower()

    # Agent-doc preflight runs BEFORE the core install: a refused
    # install must change nothing at all, and run_core mutates the
    # memory tree, mirrors, and wake files.
    mod = None
    if harness != "none":
        try:
            mod = importlib.import_module(f"smriti.integrations.{harness}.install")
        except ImportError as exc:
            print(f"error: unknown harness {harness!r} "
                  f"(no smriti.integrations.{harness}.install module): {exc}",
                  file=sys.stderr)
            return 1
        if hasattr(mod, "agent_doc_state"):
            state = mod.agent_doc_state()
            migratable = state.kind == "migration-required" and args.migrate_agent_doc
            if state.kind not in ("missing", "managed") and not migratable:
                print(f"REFUSED before any change: {state.detail}", file=sys.stderr)
                print(f"\nFAILED ({harness}): install refused — nothing "
                      "was modified.", file=sys.stderr)
                return 1

    # The privacy questions come before any change too, so that a refusal
    # (an unreadable daylog.json) or a Ctrl-C at a prompt leaves nothing done.
    try:
        plan = privacy.decide(
            memory_root,
            capture=args.capture,
            remote_private=args.remote_private,
            interactive=privacy.is_interactive(),
        )
    except privacy.PrivacyConfigError as exc:
        print(f"REFUSED before any change: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted before any change.", file=sys.stderr)
        return 130

    try:
        run_core(memory_root, projects_root, skip_recall_daemon=args.skip_recall_daemon)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        for change in privacy.apply(memory_root, plan):
            print(f"[privacy] {change}")
    except (OSError, privacy.PrivacyConfigError) as exc:
        print(f"error: couldn't record the privacy choices: {exc}", file=sys.stderr)
        return 1

    if harness == "none":
        print("\ndone (core-only). To wire into an agent harness, "
              "re-run with --harness=<name> or write your own integration.")
        print(privacy.summary(memory_root, plan))
        return 0

    # Dispatch to per-harness installer (imported above for any harness but none).
    assert mod is not None
    if harness == "claude_code":
        ok = mod.run_claude_code(
            memory_root,
            skip_settings=args.skip_settings,
            skip_mcp=args.skip_mcp,
            migrate_doc=args.migrate_agent_doc,
        )
        if not ok:
            print(f"\nFAILED ({harness}): install refused before mutating "
                  "harness state — see message above.", file=sys.stderr)
            return 1
    elif harness == "codex":
        ok = mod.run_codex(
            memory_root,
            skip_config=args.skip_config,
            migrate_doc=args.migrate_agent_doc,
        )
        if not ok:
            print(f"\nFAILED ({harness}): install refused before mutating "
                  "harness state — see message above.", file=sys.stderr)
            return 1
    else:
        # Convention for new harnesses: expose a ``run(memory_root, **opts)``.
        if not hasattr(mod, "run"):
            print(f"error: smriti.integrations.{harness}.install has no run()",
                  file=sys.stderr)
            return 1
        mod.run(memory_root)

    print(f"\ndone ({harness}). start a new session in your harness to verify.")
    print("recall config: SMRITI_RECALL_BACKEND={qmd|smriti}, "
          "SMRITI_RECALL_THRESHOLD, SMRITI_RECALL_QMD_URL, SMRITI_RECALL_NO_HTTP.")
    print("LLM provider:  SMRITI_LLM_PROVIDER={anthropic_api|claude_cli|openai_api|ollama}.")
    print("optional: `smriti recall index` to (re)build the qmd index of the memory root "
          "(`smriti status` shows which).")
    print(privacy.summary(memory_root, plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())

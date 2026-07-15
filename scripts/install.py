"""install.py — idempotent installer dispatcher for smriti.

Runs the harness-neutral core install (memory tree, wake templates,
mirror junctions, qmd recall daemon), then dispatches to a per-harness
installer chosen via ``--harness``.

Defaults to ``--harness=claude_code`` for back-compat with prior
documentation. Pass ``--harness=none`` to install only the core (useful
when wiring smriti into a custom Python agent — see
``examples/python_agent.py``).

The actual install logic lives in:
    src/smriti/install/core.py                          (core)
    src/smriti/integrations/<harness>/install.py        (per-harness)

Run on a fresh machine after ``pip install -e .``::

    python scripts/install.py
    python scripts/install.py --harness none
    python scripts/install.py --harness claude_code --skip-settings

Re-runnable: idempotent, never deletes existing user files.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from smriti.install.core import (
    DEFAULT_MEMORY_ROOT,
    DEFAULT_PROJECTS_ROOT,
    run_core,
)


KNOWN_HARNESSES = ("claude_code", "codex", "hermes", "none")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memory-root",
        default=str(DEFAULT_MEMORY_ROOT),
        help="Entity memory root (default: ~/.narada/)",
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
    args = parser.parse_args()

    memory_root = Path(args.memory_root).expanduser()
    projects_root = Path(args.projects_root).expanduser()
    harness = args.harness.strip().lower()

    try:
        run_core(memory_root, projects_root, skip_recall_daemon=args.skip_recall_daemon)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if harness == "none":
        print("\ndone (core-only). To wire into an agent harness, "
              "re-run with --harness=<name> or write your own integration.")
        return 0

    # Dispatch to per-harness installer.
    try:
        mod = importlib.import_module(f"smriti.integrations.{harness}.install")
    except ImportError as exc:
        print(f"error: unknown harness {harness!r} "
              f"(no smriti.integrations.{harness}.install module): {exc}",
              file=sys.stderr)
        return 1

    if harness == "claude_code":
        mod.run_claude_code(
            memory_root,
            skip_settings=args.skip_settings,
            skip_mcp=args.skip_mcp,
        )
    elif harness == "codex":
        mod.run_codex(memory_root, skip_config=args.skip_config)
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
    print("optional: `smriti recall index` to (re)build the qmd index for ~/.narada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

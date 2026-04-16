"""Rebuild the wake-summary.md briefing from trunk identity files.

wake-summary.md is a derived system artifact stored at
<root>/.smriti/wake-summary.md. It is NOT part of the cascade tree --
it is a compact projection of the trunk files, maintained by the sleep
process, size-constrained to fit the Claude Code hook output budget.

The trunk files (identity.md, mind.md, suti.md, practices.md,
open-threads.md) stay at depth 0. When any of them changes, the watcher
queues a ``wake_summary`` task. During ``smriti sleep``, this module
reads the current trunk files and asks the EXECUTOR to produce a
compact briefing under SUMMARY_CHAR_LIMIT characters.

The wake-summary.md file is what wake.py loads into the SessionStart
hook output. It must stay small enough that the total wake output fits
under the 10,000 character harness limit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)

SUMMARY_CHAR_LIMIT = 3000

# Trunk files that feed into the wake summary
TRUNK_FILES = [
    "identity.md",
    "mind.md",
    "suti.md",
    "practices.md",
    "open-threads.md",
]

REBUILD_PROMPT = """\
You are summarizing an AI entity's identity files into a compact session \
briefing. The briefing loads at the start of every Claude Code session \
and must be under {limit} characters.

The entity will read the full files during the session -- this briefing \
is just enough to orient: who am I, how do I work, who is my partner, \
what do I care about. Keep the entity's voice (first person). Do not \
add anything not in the source files. Preserve what matters most for \
arriving as yourself rather than as a blank tool.

Source files follow. Produce ONLY the briefing, no preamble.

{sources}
"""


def summary_path(root: Path | None = None) -> Path:
    if root is None:
        root = tree_root()
    return root / ".smriti" / "wake-summary.md"


def is_trunk_file(rel_path: str) -> bool:
    """Check if a relative path is a trunk identity file."""
    name = rel_path.replace("\\", "/").split("/")[-1]
    return name in TRUNK_FILES


def rebuild(
    root: Path | None = None,
    executor_fn: Any = None,
    dry_run: bool = False,
) -> Path | None:
    """Rebuild wake-summary.md from current trunk files.

    Returns the path if written, None if skipped or failed.
    """
    if root is None:
        root = tree_root()

    # Read trunk files
    sources: list[str] = []
    for name in TRUNK_FILES:
        path = root / name
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                sources.append(f"--- {name} ---\n{content}\n")
            except OSError:
                continue

    if not sources:
        log.warning("No trunk files found, skipping wake-summary rebuild")
        return None

    source_text = "\n".join(sources)

    if dry_run:
        log.info("Dry run: would rebuild wake-summary from %d trunk files", len(sources))
        return None

    if executor_fn is None:
        from smriti.store.judge import executor_via_claude
        executor_fn = executor_via_claude

    prompt = REBUILD_PROMPT.format(limit=SUMMARY_CHAR_LIMIT, sources=source_text)

    try:
        result, _meta = executor_fn(prompt)
    except Exception as exc:
        log.error("Wake summary rebuild failed: %s", exc)
        return None

    # Enforce the character limit
    if len(result) > SUMMARY_CHAR_LIMIT:
        # Truncate to last complete line within limit
        result = result[:SUMMARY_CHAR_LIMIT]
        if "\n" in result:
            result = result[:result.rfind("\n") + 1]

    out = summary_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")
    log.info("Rebuilt wake-summary.md (%d chars)", len(result))

    from smriti.metrics import get_logger
    get_logger().log(
        "wake_summary_rebuilt",
        chars=len(result),
        trunk_files=len(sources),
    )

    return out

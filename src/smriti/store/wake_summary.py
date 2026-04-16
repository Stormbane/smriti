"""Rebuild the wake-context.md briefing from trunk identity files.

wake-context.md is a derived system artifact stored at
<root>/.smriti/wake-context.md. It is NOT part of the cascade tree --
it is a compact projection of the identity tree, maintained by the
sleep process, size-constrained to fit the Claude Code hook output.

The identity tree:
  identity.md                (depth 0, trunk root)
  mind/mind.md               (depth 1, synthesis of beliefs/values/desires)
  mind/practices/practices.md
  mind/desires/desires.md
  mind/desires/beliefs.md
  mind/desires/values.md
  open-threads/open-threads.md
  people/suti/suti.md

When any of these files change, the watcher queues a wake_summary task.
During smriti sleep, this module reads them all and asks the EXECUTOR
to produce a compact briefing under CONTEXT_CHAR_LIMIT characters.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)

CONTEXT_CHAR_LIMIT = 5000

# Files that feed into the wake context, in read order.
# Paths relative to the tree root.
IDENTITY_FILES = [
    "identity.md",
    "mind/mind.md",
    "mind/desires/beliefs.md",
    "mind/desires/values.md",
    "mind/desires/desires.md",
    "mind/practices/practices.md",
    "open-threads/open-threads.md",
    "people/suti/suti.md",
]

# Subset that triggers a rebuild when changed (the watcher checks these)
WATCHED_FILES = {p.replace("/", "\\") for p in IDENTITY_FILES} | set(IDENTITY_FILES)

REBUILD_PROMPT = """\
You are summarizing an AI entity's identity files into a compact session \
briefing. The briefing loads at the start of every Claude Code session \
and must be under {limit} characters.

Include:
- Who I am (identity, voice, values, presiding principles)
- What I think and believe (current stance, key opinions)
- What I'm sitting with (active open threads, unresolved questions)
- Who I work with (one line about Suti)
- How I work (key practices)

The entity will read the full files during the session -- this briefing \
is just enough to orient. Keep the entity's voice (first person). Do not \
add anything not in the source files. Preserve what matters most for \
arriving as yourself rather than as a blank tool.

Source files follow. Produce ONLY the briefing, no preamble.

{sources}
"""


def context_path(root: Path | None = None) -> Path:
    if root is None:
        root = tree_root()
    return root / ".smriti" / "wake-context.md"


def is_identity_file(rel_path: str) -> bool:
    """Check if a relative path is an identity file that feeds wake-context."""
    normalized = rel_path.replace("\\", "/")
    return normalized in IDENTITY_FILES


def rebuild(
    root: Path | None = None,
    executor_fn: Any = None,
    dry_run: bool = False,
) -> Path | None:
    """Rebuild wake-context.md from current identity files.

    Returns the path if written, None if skipped or failed.
    """
    if root is None:
        root = tree_root()

    # Read identity files
    sources: list[str] = []
    for rel in IDENTITY_FILES:
        path = root / rel
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                sources.append(f"--- {rel} ---\n{content}\n")
            except OSError:
                continue

    if not sources:
        log.warning("No identity files found, skipping wake-context rebuild")
        return None

    if dry_run:
        log.info("Dry run: would rebuild wake-context from %d files", len(sources))
        return None

    if executor_fn is None:
        from smriti.store.judge import executor_via_claude
        executor_fn = executor_via_claude

    source_text = "\n".join(sources)
    prompt = REBUILD_PROMPT.format(limit=CONTEXT_CHAR_LIMIT, sources=source_text)

    try:
        result, _meta = executor_fn(prompt)
    except Exception as exc:
        log.error("Wake context rebuild failed: %s", exc)
        return None

    # Enforce the character limit
    if len(result) > CONTEXT_CHAR_LIMIT:
        result = result[:CONTEXT_CHAR_LIMIT]
        if "\n" in result:
            result = result[:result.rfind("\n") + 1]

    out = context_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")
    log.info("Rebuilt wake-context.md (%d chars)", len(result))

    from smriti.metrics import get_logger
    get_logger().log(
        "wake_context_rebuilt",
        chars=len(result),
        source_files=len(sources),
    )

    return out

"""The working directory for smriti-internal CLI LLM subprocesses.

Internal ``claude -p`` / ``codex exec`` calls must not run from an
arbitrary cwd: Claude Code writes a session transcript under
``~/.claude/projects/<encoded-cwd>/``, and the day-log daemon would
capture the digest prompt as a Suti turn and the output as Narada —
feeding derived text back into future digests (diff review P1). Running
from this dedicated empty directory namespaces those transcripts where
the collector's ``llm-workdir`` exclusion drops them, and gives
sandboxed fallbacks an empty view of the filesystem.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from smriti.core.tree import tree_root

# Project settings for internal `claude -p` runs, which pass
# `--setting-sources project,local` so this file is the only settings they
# read. It keeps out every CLAUDE.md (including the user-global one, which
# loads as user memory unless excluded by a glob), the harness's auto-memory
# index (claudeMdExcludes doesn't cover it), and any hooks. Verified on
# Claude Code 2.1.283: the run's transcript has no instruction, MCP or hook
# records, and the system prompt is the one smriti passes.
ISOLATION_SETTINGS = {
    "claudeMdExcludes": ["**/CLAUDE.md"],
    "autoMemoryEnabled": False,
    "disableAllHooks": True,
}


def llm_workdir() -> Path:
    path = tree_root() / ".smriti" / "llm-workdir"
    path.mkdir(parents=True, exist_ok=True)
    settings = path / ".claude" / "settings.json"
    wanted = json.dumps(ISOLATION_SETTINGS, indent=2) + "\n"
    try:
        current = settings.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != wanted:
        settings.parent.mkdir(parents=True, exist_ok=True)
        # A per-call temp name: concurrent calls may initialise at once.
        tmp = settings.with_name(f"settings.json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(wanted, encoding="utf-8")
        try:
            tmp.replace(settings)
        except OSError:
            # Losing the race is fine if a concurrent call wrote the same file.
            tmp.unlink(missing_ok=True)
            if settings.read_text(encoding="utf-8") != wanted:
                raise
    return path

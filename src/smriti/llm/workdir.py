"""The working directory for smriti-internal CLI LLM subprocesses.

Internal ``claude -p`` / ``codex exec`` calls must not run from an
arbitrary cwd: Claude Code writes a session transcript under
``~/.claude/projects/<encoded-cwd>/``, and the day-log daemon would
capture the digest prompt as a user turn and the output as the agent's —
feeding derived text back into future digests. Running
from this dedicated empty directory namespaces those transcripts where
the collector's ``llm-workdir`` exclusion drops them, and gives
sandboxed fallbacks an empty view of the filesystem.
"""

from __future__ import annotations

import json
import os
import time
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
            _replace_with_retry(tmp, settings, wanted)
        finally:
            tmp.unlink(missing_ok=True)
    return path


def _replace_with_retry(tmp: Path, target: Path, wanted: str, attempts: int = 20) -> None:
    """Atomically install *tmp* as *target*, tolerating concurrent writers.

    On Windows a replace onto a file another thread is replacing or reading
    fails with "access denied", and so can a read in that instant. Losing
    the race is success as soon as the target holds the wanted content.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            tmp.replace(target)
            return
        except OSError as exc:
            last = exc
        try:
            if target.read_text(encoding="utf-8") == wanted:
                return
        except OSError:
            pass
        time.sleep(0.01 * (attempt + 1))
    raise OSError(f"could not install {target}") from last

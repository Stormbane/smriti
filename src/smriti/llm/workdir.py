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

from pathlib import Path

from smriti.core.tree import tree_root


def llm_workdir() -> Path:
    path = tree_root() / ".smriti" / "llm-workdir"
    path.mkdir(parents=True, exist_ok=True)
    return path

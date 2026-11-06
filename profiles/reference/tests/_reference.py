"""Enable the reference profile in a test memory root, as its installation's daylog.json does."""

from __future__ import annotations

import json
from pathlib import Path

# What the reference installation's daylog.json sets to keep its behaviour.
REFERENCE_CONFIG: dict[str, object] = {"profile": "reference", "user_name": "Suti",
                                      "agent_name": "Narada", "morning_web_search": True,
                                      "nightly_fallback": "codex_cli"}


def enable_profile(root: Path, **config: object) -> Path:
    """Merge *config* into ``<root>/.smriti/daylog.json``; returns the file."""
    path = root / ".smriti" / "daylog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    path.write_text(json.dumps({**current, **config}), encoding="utf-8")
    return path


def enable_reference(root: Path) -> Path:
    return enable_profile(root, **REFERENCE_CONFIG)

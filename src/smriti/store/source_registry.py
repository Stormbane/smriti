"""Source registry: provenance of files rolled into concept pages.

`~/.narada/.smriti/consolidated.json` maps relative leaf path -> sha256 of
the source content at the time `batch_consolidate` used it. Two purposes:

- `find_pending_ingest` uses this to tell "file never consolidated" apart
  from "file edited since consolidation" -- the latter should be re-queued,
  the former is the first-time case.
- Debugging: trace any concept page back to its source files.

Registry is append-only at the semantic level (we never delete entries for
files that still exist on disk). Missing entries = never consolidated.
Entries with stale sha256 = source edited since consolidation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)


def _registry_path(root: Path | None = None) -> Path:
    if root is None:
        root = tree_root()
    return root / ".smriti" / "consolidated.json"


def load_registry(root: Path | None = None) -> dict[str, str]:
    """Return {rel_path: sha256} map. Empty dict if file missing."""
    p = _registry_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Registry load failed (%s), starting fresh", exc)
        return {}


def save_registry(registry: dict[str, str], root: Path | None = None) -> None:
    p = _registry_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str | None:
    """Compute sha256 of file content. None on read error."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def mark_consolidated(
    paths: list[Path],
    root: Path | None = None,
) -> int:
    """Record paths as consolidated with their current sha256. Returns count updated."""
    if root is None:
        root = tree_root()

    registry = load_registry(root)
    updated = 0
    for p in paths:
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            continue
        sha = file_sha256(p)
        if sha is None:
            continue
        if registry.get(rel) != sha:
            registry[rel] = sha
            updated += 1
    if updated:
        save_registry(registry, root)
        log.info("Source registry: updated %d entries", updated)
    return updated


def needs_consolidation(path: Path, root: Path | None = None, registry: dict[str, str] | None = None) -> bool:
    """True if path is absent from registry, or file content has changed since."""
    if root is None:
        root = tree_root()
    if registry is None:
        registry = load_registry(root)

    try:
        rel = str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return False

    stored = registry.get(rel)
    if stored is None:
        return True
    current = file_sha256(path)
    return current != stored

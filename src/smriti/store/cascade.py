"""Structural cascade: regenerate parent index.md files from directory listings.

When a file is written, every existing index.md between it and the tree
root is regenerated. No LLM is involved.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from smriti.core.tree import tree_root

log = logging.getLogger(__name__)


def _generate_index(directory: Path, root: Path) -> str:
    """Generate an index.md body from a directory's contents."""
    entries = sorted(directory.iterdir())
    lines = [f"# {directory.name.replace('-', ' ').title()}", ""]

    for entry in entries:
        if entry.name.startswith(".") or entry.name == "index.md":
            continue

        rel = str(entry.relative_to(root)).replace("\\", "/")
        name = entry.stem.replace("-", " ").replace("_", " ")

        if entry.is_dir():
            lines.append(f"- [[{rel}/]] — {name}")
        elif entry.suffix == ".md":
            rel_no_ext = str(entry.relative_to(root).with_suffix("")).replace("\\", "/")
            # Try to extract the first heading
            try:
                text = entry.read_text(encoding="utf-8")
                heading_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
                desc = heading_match.group(1) if heading_match else name
            except OSError:
                desc = name
            lines.append(f"- [[{rel_no_ext}]] — {desc}")

    lines.append("")
    return "\n".join(lines)


def structural_cascade(path: Path, root: Path | None = None) -> list[Path]:
    """Regenerate parent index.md files up the tree.

    Returns the list of index.md files that were updated.
    Does NOT touch MEMORY.md or trunk-level files.
    """
    import time

    t0 = time.monotonic()

    if root is None:
        root = tree_root()

    updated: list[Path] = []
    current = path.parent if path.is_file() else path

    from smriti.core.files import edit_lock, write_atomic

    while current != root and current.is_relative_to(root):
        index_file = current / "index.md"
        if index_file.exists():
            # Listed under the lock, so of two writers in one directory the
            # later one lists both files and its index is the one that stays.
            with edit_lock(index_file):
                new_content = _generate_index(current, root)
                old_content = index_file.read_text(encoding="utf-8")
                changed = new_content.strip() != old_content.strip()
                if changed:
                    write_atomic(index_file, new_content)
            if changed:
                updated.append(index_file)
                log.info("Structural cascade: updated %s", index_file.relative_to(root))

        current = current.parent

    from smriti.metrics import get_logger
    get_logger().log("structural_cascade", trigger_path=str(path.relative_to(root)), indexes_updated=len(updated), elapsed_ms=int((time.monotonic() - t0) * 1000))
    return updated

"""Keep each directory's index.md listing in step with its files.

When an entry is written, every existing ``index.md`` between it and the
tree root is regenerated from its directory's listing: one wikilink per
subdirectory and markdown file, described by the file's first heading.
Deterministic; no LLM is involved.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from smriti.core.files import edit_lock
from smriti.core.memwrite import mask, write_memory
from smriti.core.tree import tree_root
from smriti.metrics import get_logger

log = logging.getLogger(__name__)


def _render_index(directory: Path, root: Path) -> str:
    """Render an index.md body from a directory's contents."""
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


def update_index_pages(path: Path, root: Path | None = None) -> list[Path]:
    """Regenerate the index.md of each directory from *path* up to *root*.

    Only directories that already have an index.md are updated; none is
    created, and the root's own directory is never touched. Returns the
    index.md files whose content changed.
    """
    t0 = time.monotonic()

    if root is None:
        root = tree_root()

    updated: list[Path] = []
    current = path.parent if path.is_file() else path

    while current != root and current.is_relative_to(root):
        index_file = current / "index.md"
        if index_file.exists():
            # Listed under the lock, so of two writers in one directory the
            # later one lists both files and its index is the one that stays.
            with edit_lock(index_file):
                new_content = _render_index(current, root)
                old_content = index_file.read_text(encoding="utf-8")
                changed = new_content.strip() != old_content.strip()
                if changed:
                    # A listed heading can carry a credential shape (a file
                    # written by hand): the listing as written is masked, and
                    # is a change only if it differs once masked.
                    new_content = mask(new_content)
                    changed = new_content.strip() != old_content.strip()
                if changed:
                    write_memory(index_file, new_content)
            if changed:
                updated.append(index_file)
                log.info("Updated index page %s", index_file.relative_to(root))

        current = current.parent

    get_logger().log(
        "index_pages_updated",
        trigger_path=str(path.relative_to(root)),
        indexes_updated=len(updated),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )
    return updated

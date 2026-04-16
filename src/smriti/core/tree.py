"""Tree scanning, trunk-distance computation, and path helpers.

The tree root defaults to ``~/.narada`` (overridable via ``SMRITI_ROOT``
or the legacy ``NARADA_ROOT`` environment variable).  Trunk distance is
the number of path components between a file and the tree root — a rough
proxy for how "identity-adjacent" a file is.  Root-level files
(identity.md) have distance 0; ``mind/mind.md`` has distance 1;
``mind/desires/beliefs.md`` has distance 2; and so on.
"""

from __future__ import annotations

import os
from pathlib import Path


def tree_root() -> Path:
    """Return the resolved tree root, defaulting to ``~/.narada``."""
    env = os.environ.get("SMRITI_ROOT") or os.environ.get("NARADA_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".narada"


def smriti_db_path() -> Path:
    """Return the path to the smriti index database."""
    return tree_root() / ".smriti" / "index.db"


def trunk_distance(path: Path, root: Path | None = None) -> int:
    """Compute the trunk distance of *path* relative to *root*.

    Distance is the number of directory components between the file and the
    tree root.  Files directly in the root have distance 0.

    Returns -1 if *path* is not under *root*.
    """
    if root is None:
        root = tree_root()

    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return -1

    # Number of parent directories (not counting the filename itself).
    return len(rel.parts) - 1

"""Tree scanning, trunk-distance computation, and path helpers.

The tree root (the memory root) is, in order: ``SMRITI_ROOT`` or the
legacy ``NARADA_ROOT`` when set; else ``~/.narada`` when it is a
directory, the root's name before 0.2, so an existing install keeps its
tree; else ``~/.smriti``. Trunk distance is the number of path
components between a file and the tree root — a rough proxy for how
"identity-adjacent" a file is.  Root-level files (identity.md) have
distance 0; ``mind/mind.md`` has distance 1; ``mind/desires/beliefs.md``
has distance 2; and so on.
"""

from __future__ import annotations

import os
from pathlib import Path


_ROOT_VARIABLES = ("SMRITI_ROOT", "NARADA_ROOT")
DEFAULT_ROOT_NAME = ".smriti"
LEGACY_ROOT_NAME = ".narada"  # the default root before 0.2


def _choose_root() -> tuple[Path, str]:
    """The tree root and the rule that chose it (see :func:`tree_root_source`)."""
    for name in _ROOT_VARIABLES:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve(), name
    home = Path.home()
    legacy = home / LEGACY_ROOT_NAME
    # Checked first, so an install made before 0.2 never moves to a new,
    # empty ~/.smriti, even when one appears beside it.
    if legacy.is_dir():
        return legacy, f"legacy ~/{LEGACY_ROOT_NAME}"
    return home / DEFAULT_ROOT_NAME, f"default ~/{DEFAULT_ROOT_NAME}"


def tree_root() -> Path:
    """The memory root, chosen at each call by the rules in the module docstring."""
    return _choose_root()[0]


def tree_root_source() -> str:
    """Which rule chose :func:`tree_root`, as ``smriti status`` shows it.

    One of ``"SMRITI_ROOT"``, ``"NARADA_ROOT"``, ``"legacy ~/.narada"``
    and ``"default ~/.smriti"``.
    """
    return _choose_root()[1]


def tree_root_warning() -> str:
    """Why the chosen root may not be the one meant, or "" when there is no doubt.

    With no variable set and both ``~/.narada`` and ``~/.smriti`` present,
    the legacy one is used and the other is ignored; say so.
    """
    if any(os.environ.get(name) for name in _ROOT_VARIABLES):
        return ""
    home = Path.home()
    if (home / LEGACY_ROOT_NAME).is_dir() and (home / DEFAULT_ROOT_NAME).exists():
        return (f"both ~/{LEGACY_ROOT_NAME} and ~/{DEFAULT_ROOT_NAME} exist; using "
                f"~/{LEGACY_ROOT_NAME} (set SMRITI_ROOT to choose)")
    return ""


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

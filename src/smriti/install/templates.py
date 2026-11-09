"""Where the installer finds the files it copies: in the package, or in the checkout.

The installer copies three things it doesn't generate: the scripts deployed
into ``<root>/.smriti/`` (``narada/.smriti/``: wake.py and
narada-p.sh), the memory tree's skeleton (``memory_template/``), and it names
``docs/PRIVACY.md``. They are kept at the top of the checkout, where the
documentation points and where the installer's auto-upgrade compares a
deployed script against.

A wheel carries copies of them in the package, under ``smriti/_data/`` at
the same relative paths (``[tool.hatch.build.targets.wheel.force-include]``
in pyproject.toml maps them), and they are read through
:mod:`importlib.resources`. An editable install or a checkout on the path
has no ``_data``, so the checkout's own files are used, and only when the
package really sits in a checkout's ``src/``: a package installed elsewhere
never reads files outside itself.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

# The package directory the wheel's copies live under.
PACKAGED_DIR = "_data"

# Each template, by its path in the checkout (and under PACKAGED_DIR).
SCRIPTS = "narada/.smriti"
MEMORY_TEMPLATE = "memory_template"
PRIVACY_DOC = "docs/PRIVACY.md"
ALL = (SCRIPTS, MEMORY_TEMPLATE, PRIVACY_DOC)

_PACKAGE_DIR = Path(__file__).resolve().parents[1]  # .../smriti
_CHECKOUT = _PACKAGE_DIR.parents[1]  # <checkout>, when the package is <checkout>/src/smriti


def packaged(relative: str) -> Path | None:
    """The package's own copy of *relative*, or None when the package carries none.

    A package imported from a zip has no file path to copy from, and counts
    as carrying none.
    """
    found = resources.files("smriti").joinpath(PACKAGED_DIR, *relative.split("/"))
    if isinstance(found, Path) and found.exists():
        return found
    return None


def checkout() -> Path | None:
    """The checkout the package is imported from, or None when it isn't one."""
    if _PACKAGE_DIR.parent.name == "src" and (_CHECKOUT / "pyproject.toml").is_file():
        return _CHECKOUT
    return None


def template(relative: str) -> Path | None:
    """The file or directory *relative* names (one of :data:`ALL`).

    The packaged copy when there is one (an installed wheel), else the
    checkout's (an editable install, or a checkout on the path), else None.
    """
    found = packaged(relative)
    if found is not None:
        return found
    repo = checkout()
    if repo is not None and (repo / relative).exists():
        return repo / relative
    return None

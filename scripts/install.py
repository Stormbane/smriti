"""install.py — the installer, run from a checkout: ``python scripts/install.py``.

The installer lives in the package (:mod:`smriti.install.cli`), so a wheel
install runs it as ``python -m smriti.install``; this script runs the same
thing, with the same flags (``--help`` lists them)::

    python scripts/install.py
    python scripts/install.py --harness none
    python scripts/install.py --harness claude_code --skip-settings
    python scripts/install.py --no-capture --remote-is-private
"""

from __future__ import annotations

import sys

from smriti.install.cli import main

if __name__ == "__main__":
    sys.exit(main())

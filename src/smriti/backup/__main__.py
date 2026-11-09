"""``python -m smriti.backup``: one backup run (:mod:`smriti.backup.run`)."""

from __future__ import annotations

import sys

from smriti.backup.run import main

if __name__ == "__main__":
    sys.exit(main())

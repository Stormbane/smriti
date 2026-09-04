"""``python -m smriti`` — same entry as the ``smriti`` console script.

Scheduled tasks invoke this form so they bind to a specific
interpreter rather than whatever ``smriti`` shim is first on PATH.
"""

from __future__ import annotations

import sys

from smriti.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""The credential scrub, re-exported from :mod:`smriti.core.scrub`, its home.

It moved to ``core`` because the memory writes there use it, and ``core``
imports nothing from the packages above it. This name keeps imports of
``smriti.daylog.scrub`` working, the reference profile's among them.
"""

from smriti.core.scrub import scrub as scrub

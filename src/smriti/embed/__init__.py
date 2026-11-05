"""Embeddings for the index: what made a vector, and one model per machine.

- :mod:`smriti.embed.identity`: the embedding identity an index is built
  with, recorded in its ``meta`` table with the first vectors and checked
  wherever vectors are written or compared.

Everything here is stdlib-only and imports no embedding runtime, so a
long-lived process can use it without loading a model.
"""

from __future__ import annotations

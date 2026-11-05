"""Embeddings for the index: what made a vector, and one model per machine.

- :mod:`smriti.embed.identity`: the embedding identity an index is built
  with, recorded in its ``meta`` table with the first vectors and checked
  wherever vectors are written or compared.
- :mod:`smriti.embed.service`: ``smriti embed serve``, one process per
  memory root that holds the model and answers embedding requests over
  loopback TCP; :mod:`smriti.embed.client` asks it, starting it when needed.
- :mod:`smriti.embed.protocol`: framing, bounds and mutual authentication.
- :mod:`smriti.embed.runtime`: the private per-user runtime directory, its
  key, and each service's lock, discovery file and log.

Everything here is stdlib-only and imports no embedding runtime, so a
long-lived process can use it without loading a model (the service loads
one, through the vendored providers).
"""

from __future__ import annotations

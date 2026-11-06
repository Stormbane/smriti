"""Embeddings for the index: what made a vector, and one model per machine.

- :mod:`smriti.embed.identity`: the embedding identity an index is built
  with, recorded in its ``meta`` table with the first vectors and checked
  wherever vectors are written or compared.
- :mod:`smriti.embed.provider`: which model a process embeds with (the one
  provider rule) and the embedding call, shared by the indexer, search and
  the service.
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

_holding = False


def hold_while_running() -> None:
    """Keep the memory root's embedding service loaded while this process runs.

    For a long-lived client, the MCP server: from its first embedding on,
    its client holds the service (:class:`smriti.embed.client.ServiceEmbedding`),
    so a search after a long pause still finds the model loaded. Short-lived
    processes (``smriti read``, hooks, the nightly) don't call it, so they
    never keep the model in memory. The server calls it at its first tool
    call, so its startup imports no more than before.
    """
    global _holding
    _holding = True


def holding() -> bool:
    """Whether this process holds the service (see :func:`hold_while_running`)."""
    return _holding

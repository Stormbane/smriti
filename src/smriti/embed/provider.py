"""Which embedding model a process uses, and embedding with it.

One API for the indexer, search and the embedding service:

- :func:`provider_keys` is the one rule for which provider loads.
- :func:`embedder` is what a search or a write embeds with: the memory
  root's embedding service in a long-lived process, or the model loaded in
  this process (:func:`local_provider`) when the service is off
  (``SMRITI_EMBED_SERVICE=0``) or inside :func:`in_process_embeddings`.
- :func:`embed` embeds texts and returns the vectors with the identity that
  made them; :func:`failure_reason` says why it couldn't.

Importing this module loads no model and imports no embedding runtime: the
vendored providers are imported when one loads, the service client when it
is asked.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from smriti.embed.identity import (
    Embedded,
    EmbeddingUnavailable,
    StaleEmbedding,
    check_vectors,
    identity_of,
    provider_for_model,
)

log = logging.getLogger(__name__)

# Registry keys tried when nothing pins the choice, best-first: onnx is
# lighter and offline-first.
AUTO_ORDER = ("onnx", "local")
OVERRIDE_ENV = "SMRITI_EMBED_PROVIDER"


class EmbeddingProvider(Protocol):
    """The embedding interface used here, across the untyped vendor boundary."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def recorded_provider(db: Path | None = None) -> str | None:
    """The provider the existing index was built with, from its meta table, or None.

    Every process embedding against an existing index must load the SAME
    provider: a different one at best mismatches the vector dimension
    (vec search silently degrades to FTS) and at worst mixes native
    runtimes: trying onnxruntime first and then importing torch into the
    same process aborted intermittently with 0xC0000409 mid-model-load,
    which the harness surfaced as "Connection closed" (2026-09-06).
    """
    if db is None:
        from smriti.core.tree import smriti_db_path

        db = smriti_db_path()
    if not db.exists():
        return None
    try:
        from smriti.store.schema import open_readonly

        conn = open_readonly(db, vectors=False)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'embedding_provider'"
            ).fetchone()
            if row:
                return cast(str | None, row[0])
            # Indexes built before the provider was recorded (< e8d98c2)
            # still name their model: infer the provider from it rather
            # than let the auto order pick a different one and strand
            # the vectors behind the dimension guard.
            row = conn.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
            return provider_for_model(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not read the index's embedding provider from %s: %s", db, exc)
        return None


_overrides_noted: set[tuple[str, str]] = set()


def provider_keys(db: Path | None = None, *, rebuild: bool = False) -> tuple[str, ...]:
    """The provider keys to try, best first: the one rule for choosing an embedding model.

    1. The provider the index at ``db`` records, for everything that serves
       the existing index: searches, writes, the repairs, the embedding
       service and an incremental ``smriti index``. Only vectors of that
       model can be compared with, or stored beside, the index's.
    2. ``SMRITI_EMBED_PROVIDER``, when nothing is recorded (a new or empty
       index), or when the index is rebuilt (``rebuild``: ``smriti index
       --full``, which migrates the index to that provider).
    3. The auto order (:data:`AUTO_ORDER`).

    So the override never makes a process embed against an index of another
    model: before H12.5 the indexer let it win and the service didn't.
    A rebuild without the override keeps the recorded provider.
    """
    override = os.environ.get(OVERRIDE_ENV, "").strip().lower()
    if rebuild and override:
        return (override,)
    recorded = recorded_provider(db)
    if recorded:
        if override and override != recorded and (override, recorded) not in _overrides_noted:
            _overrides_noted.add((override, recorded))
            log.warning("%s=%s is not used: the index records %s, and only "
                        "'smriti index --full' migrates it", OVERRIDE_ENV, override, recorded)
        return (recorded,)
    if override:
        return (override,)
    return AUTO_ORDER


class InProcess:
    """A provider loaded in this process, with its identity worked out once, at load."""

    def __init__(self, inner: EmbeddingProvider) -> None:
        self._inner = inner
        self.identity = identity_of(inner)

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._inner.embed(texts)


def load_provider(keys: tuple[str, ...]) -> InProcess:
    """Load the first of ``keys`` that loads (5-18 s: a model load); RuntimeError if none."""
    from smriti._vendored.memsearch.embeddings import get_provider

    last_error: Exception | None = None
    for key in keys:
        try:
            return InProcess(cast(EmbeddingProvider, get_provider(key)))
        except Exception as exc:  # noqa: BLE001 — logged; the next provider is tried
            last_error = exc
            log.warning("embedding provider %r unavailable: %s", key, exc)
    raise RuntimeError(
        f"No embedding provider available (tried {', '.join(keys)}; "
        f"last error: {last_error}). The default ONNX provider needs "
        "onnxruntime, tokenizers and huggingface_hub, all in "
        "pip install 'smriti[read]'; the local provider needs sentence-transformers."
    )


@dataclass(frozen=True)
class _Scope:
    db: Path | None
    rebuild: bool


_IN_PROCESS: ContextVar[_Scope | None] = ContextVar("smriti_in_process_embeddings",
                                                    default=None)
_ROOT: ContextVar[Path | None] = ContextVar("smriti_embedding_root", default=None)


@contextmanager
def embedding_root(root: Path) -> Iterator[None]:
    """Embed for the memory root ``root`` for the block, whatever the environment's is.

    :func:`embedder` is then ``root``'s embedding service, or a model chosen
    by the provider ``root``'s index records. ``index_file`` runs this way,
    so a write to another root never embeds with, or starts, the service of
    the root in ``SMRITI_ROOT``.
    """
    token = _ROOT.set(root)
    try:
        yield
    finally:
        _ROOT.reset(token)


@contextmanager
def in_process_embeddings(*, db: Path | None = None, rebuild: bool = False) -> Iterator[None]:
    """Embed with a model loaded in this process for the block, not through the service.

    ``index_tree`` runs this way: it embeds thousands of chunks in one
    short-lived process, where loading the model once costs less than
    shipping every chunk to the service. ``db`` is the index the block
    serves (by default the memory root's), and ``rebuild`` says the block
    builds it anew, where :func:`provider_keys` lets the override choose.
    """
    token = _IN_PROCESS.set(_Scope(db, rebuild))
    try:
        yield
    finally:
        _IN_PROCESS.reset(token)


def embeds_through_service() -> bool:
    """Whether :func:`embedder` is the service here (whose own loop fills pending vectors)."""
    if _IN_PROCESS.get() is not None:
        return False
    from smriti.embed.client import service_enabled

    return service_enabled()


def embedder() -> EmbeddingProvider:
    """The embedder for this process's searches and writes.

    In a long-lived process (the MCP server, ``smriti read``, hooks) that
    is the memory root's shared embedding service, so no process but the
    service loads a model (``smriti.embed``). ``SMRITI_EMBED_SERVICE=0``,
    or :func:`in_process_embeddings`, loads one here instead. The memory
    root is the environment's, or the one :func:`embedding_root` names.
    """
    if embeds_through_service():
        from smriti.embed.client import service_provider

        root = _ROOT.get()
        if root is None:
            from smriti.core.tree import tree_root

            root = tree_root()
        return service_provider(root)
    return local_provider()


_loaded: InProcess | None = None
_checked: set[Path | None] = set()  # the indexes _loaded's provider was chosen for
_load_lock = threading.Lock()


def local_provider() -> EmbeddingProvider:
    """The model loaded in this process, chosen by :func:`provider_keys` (kept until forgotten).

    Building the provider costs 5-18 s (model load); embedding a chunk
    costs ~0.02 s warm. Without the cache every write paid the build
    cost again: the bulk of smriti_write latency in long-lived MCP
    servers. One model is kept. The keys are worked out again for a
    rebuild, which loads another when the rule now names one, and for an
    index the model wasn't chosen for (another root's), which loads
    another when that index records a different provider.
    """
    global _loaded
    scope = _IN_PROCESS.get()
    rebuild = scope is not None and scope.rebuild
    db = scope.db if scope is not None else None
    if db is None and (root := _ROOT.get()) is not None:
        from smriti.core.tree import index_db_path

        db = index_db_path(root)
    with _load_lock:
        if _loaded is not None and not rebuild and db in _checked:
            return _loaded
        keys = provider_keys(db, rebuild=rebuild)
        if _loaded is None or _loaded.identity.provider not in keys:
            _loaded = load_provider(keys)
            _checked.clear()
        _checked.add(db)
        return _loaded


def forget_provider() -> None:
    """Drop the loaded model, so the next embedding loads what the index records now.

    A model cached in a long-lived process can outlive a migration of the
    index to another one; its vectors are then stale (see :func:`embed`).
    """
    global _loaded
    with _load_lock:
        _loaded = None
        _checked.clear()


def embed(provider: EmbeddingProvider, texts: list[str], kind: str = "write") -> Embedded:
    """Embed ``texts``, with the identity that made the vectors.

    A provider that knows the identity of each result (the embedding
    service's client) reports it per call; an in-process one has one
    identity for its life. ``kind`` ("query" or "write") sets the deadline
    a remote embedder works to.
    """
    per_call = getattr(provider, "embed_with_identity", None)
    if callable(per_call):
        embedded = per_call(texts, kind=kind)
        if not isinstance(embedded, Embedded):
            raise EmbeddingUnavailable("the embedder returned no identity with its vectors")
    else:
        import asyncio

        vectors = asyncio.run(provider.embed(texts)) if texts else []
        embedded = Embedded(vectors, identity_of(provider))
    return check_vectors(embedded, len(texts))


def failure_reason(exc: BaseException) -> str:
    """A failure to embed, as the user reads it in a degraded status."""
    if isinstance(exc, (EmbeddingUnavailable, StaleEmbedding)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"

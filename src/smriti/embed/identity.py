"""The embedding identity: which provider, model, model files and dimension made a vector.

An index is built with one identity. It is recorded in the index's ``meta``
table atomically with the first vectors (whoever writes them: a full index,
an incremental one, or the first ``index_file`` on an empty index), and
every embedding carries the identity that produced it. Writers check it
inside their write transaction and search checks it against the snapshot
it reads vectors from, so vectors from one model never land beside, or get
compared with, another's. That holds whether the vectors come from a model
in this process (which may have been loaded before a migration) or from
the shared embedding service.

An empty field is unknown, not a value: a provider that doesn't expose its
model files' revision, an index recorded before revisions were, a test
double whose registry key can't be told. Unknown fields match anything;
known fields must be equal.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smriti.store.schema import IndexDB

log = logging.getLogger(__name__)

# Map provider class name -> registry key: the class doesn't know its key.
_PROVIDER_KEYS = {"OnnxEmbedding": "onnx", "LocalEmbedding": "local", "StubEmbedding": "stub"}

# The meta key holding the identity as JSON. The older keys (embedding_provider,
# model, dimension) are kept up to date beside it for older readers.
META_KEY = "embedding_identity"


class EmbeddingUnavailable(RuntimeError):
    """No vectors could be made. The message says why, for the user to read."""


class StaleEmbedding(RuntimeError):
    """Vectors made with an identity other than the one the index records."""


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str  # registry key: "local", "onnx", "stub"; "" when unknown
    model: str
    revision: str  # the model files' revision (a hub snapshot hash); "" when unknown
    dimension: int  # 0 when unknown

    def compatible(self, other: EmbeddingIdentity) -> bool:
        """True when vectors of ``self`` and ``other`` may share an index."""
        def same(a: str, b: str) -> bool:
            return not a or not b or a == b

        return (same(self.provider, other.provider) and same(self.model, other.model)
                and same(self.revision, other.revision)
                and (not self.dimension or not other.dimension
                     or self.dimension == other.dimension))

    def merged(self, other: EmbeddingIdentity) -> EmbeddingIdentity:
        """``self`` with its unknown fields filled in from ``other``."""
        return EmbeddingIdentity(self.provider or other.provider, self.model or other.model,
                                 self.revision or other.revision,
                                 self.dimension or other.dimension)

    def describe(self) -> str:
        """``local all-MiniLM-L6-v2@1110a24 (384)``: short enough for a status line."""
        rev = f"@{self.revision[:7]}" if self.revision else ""
        return f"{self.provider or '?'} {self.model or '?'}{rev} ({self.dimension or '?'})"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_dict(cls, data: object) -> EmbeddingIdentity | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(str(data.get("provider") or ""), str(data.get("model") or ""),
                       str(data.get("revision") or ""), int(data.get("dimension") or 0))
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_json(cls, text: str | None) -> EmbeddingIdentity | None:
        if not text:
            return None
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError:
            return None


@dataclass(frozen=True)
class Embedded:
    """Vectors and the identity of what made them."""

    vectors: list[list[float]]
    identity: EmbeddingIdentity


def provider_key(provider: object) -> str | None:
    """Registry key ("onnx"/"local"/"stub") for a provider instance, if known."""
    return _PROVIDER_KEYS.get(type(provider).__name__)


def provider_for_model(model: str) -> str | None:
    """Legacy inference: which registry provider serves this model name."""
    name = model.lower()
    if name.startswith("stub-"):
        return "stub"
    if "bge-m3" in name:
        return "onnx"
    if "minilm" in name or "mpnet" in name or "sentence-transformers" in name:
        return "local"
    return None


def model_revision(key: str, model: str) -> str:
    """The hub snapshot (a hash of the model's files) a local provider loads, or "".

    The ``local`` and ``onnx`` providers load from the Hugging Face cache, whose
    snapshot directories are named by the repository commit they hold. Looked
    up offline; anything else (a model given as a directory, no hub library,
    a provider that doesn't load from the hub) is unknown.
    """
    if key == "local":
        repo = model if "/" in model else f"sentence-transformers/{model}"
    elif key == "onnx":
        repo = model
    else:
        return ""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return ""
    for filename in ("config.json", "tokenizer.json"):
        try:
            found = try_to_load_from_cache(repo, filename)
        except (ValueError, OSError) as exc:  # not a hub id, or an unreadable cache
            log.debug("no hub revision for %s: %s", repo, exc)
            return ""
        if isinstance(found, str):
            parts = Path(found).parts
            if "snapshots" in parts and parts.index("snapshots") + 1 < len(parts):
                return parts[parts.index("snapshots") + 1]
    return ""


def identity_of(provider: object) -> EmbeddingIdentity:
    """The identity of an in-process provider (or of anything shaped like one)."""
    known = getattr(provider, "identity", None)
    if isinstance(known, EmbeddingIdentity):
        return known
    model = str(getattr(provider, "model_name", "") or "")
    key = provider_key(provider) or provider_for_model(model) or ""
    revision = getattr(provider, "revision", None)
    if not isinstance(revision, str):
        revision = model_revision(key, model)
    return EmbeddingIdentity(key, model, revision, int(getattr(provider, "dimension", 0) or 0))


def check_vectors(embedded: Embedded, count: int) -> Embedded:
    """``embedded``, after checking it holds ``count`` vectors of its identity's size."""
    if len(embedded.vectors) != count:
        raise EmbeddingUnavailable(
            f"the embedder returned {len(embedded.vectors)} vectors for {count} texts")
    dim = embedded.identity.dimension
    if dim and any(len(v) != dim for v in embedded.vectors):
        raise EmbeddingUnavailable(f"the embedder returned vectors not of dimension {dim}")
    return embedded


# ── The index's record ───────────────────────────────────────────────


def recorded_identity(conn: IndexDB | sqlite3.Connection) -> EmbeddingIdentity | None:
    """The identity the index records, or None when it records none.

    Indexes from before the identity was recorded still name their provider,
    model and dimension in the older keys: those are the identity, with the
    revision unknown. When an older writer has changed those keys since the
    identity was recorded, they win, because they describe the newer vectors.
    """
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN (?, 'embedding_provider', 'model', "
            "'dimension')", (META_KEY,)).fetchall())
    except sqlite3.OperationalError:  # no meta table: nothing is recorded
        return None
    model = str(rows.get("model") or "")
    try:
        dim = int(rows.get("dimension") or 0)
    except ValueError:
        dim = 0
    legacy = EmbeddingIdentity(str(rows.get("embedding_provider") or "")
                               or provider_for_model(model) or "", model, "", dim)
    stored = EmbeddingIdentity.from_json(rows.get(META_KEY))
    if stored is not None and (not model or stored.model == model) and (
            not dim or stored.dimension == dim):
        return stored
    if not model and not dim:
        return None
    return legacy


def has_identity_record(conn: IndexDB | sqlite3.Connection) -> bool:
    """True when the index holds an identity written with its vectors (not just old keys)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and EmbeddingIdentity.from_json(row[0]))


def write_identity(conn: IndexDB, identity: EmbeddingIdentity) -> None:
    """Record ``identity`` in meta, with the older keys kept in step for older readers."""
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (META_KEY, identity.to_json()))
    if identity.provider:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('embedding_provider', ?)",
                     (identity.provider,))
    if identity.model:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
                     (identity.model,))
    if identity.dimension:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dimension', ?)",
                     (str(identity.dimension),))


def vec_table_dimension(conn: IndexDB | sqlite3.Connection) -> int | None:
    """The dimension ``chunks_vec`` was created with, or None when there is no such table."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'").fetchone()
    if not row:
        return None
    m = re.search(r"float\[(\d+)\]", str(row[0] or ""))
    return int(m.group(1)) if m else None


def has_vectors(conn: IndexDB) -> bool:
    if not conn.has_vec or vec_table_dimension(conn) is None:
        return False
    return conn.execute("SELECT 1 FROM chunks_vec LIMIT 1").fetchone() is not None


def mismatched_identity(conn: IndexDB, ours: EmbeddingIdentity) -> EmbeddingIdentity | None:
    """The index's identity when ``ours`` doesn't match it, else None.

    An index with neither an identity record nor vectors has no identity
    yet: its first vectors give it one.
    """
    if not (has_identity_record(conn) or has_vectors(conn)):
        return None
    recorded = recorded_identity(conn)
    return recorded if recorded is not None and not recorded.compatible(ours) else None


def bind_identity(conn: IndexDB, ours: EmbeddingIdentity) -> EmbeddingIdentity:
    """Check ``ours`` against the index inside the caller's write transaction.

    Raises :class:`StaleEmbedding` when the index records an identity that
    ``ours`` doesn't match. On an index with no recorded identity and no
    vectors, ``ours`` becomes its identity: the vector table is made the
    right size and the identity recorded, in the same transaction as the
    first vectors. Returns the identity the index has afterwards.
    """
    recorded = recorded_identity(conn)
    if recorded is not None and (has_identity_record(conn) or has_vectors(conn)):
        if not recorded.compatible(ours):
            raise StaleEmbedding(
                f"the index's embeddings are {recorded.describe()}; "
                f"these are {ours.describe()}")
        final = recorded.merged(ours)
        if final != recorded or not has_identity_record(conn):
            write_identity(conn, final)
        return final
    # The first vectors: whoever writes them names the index's identity.
    if conn.has_vec and ours.dimension and vec_table_dimension(conn) != ours.dimension:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{ours.dimension}])")
    write_identity(conn, ours)
    return ours

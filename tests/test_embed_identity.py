"""H10.1: the embedding identity, and vectors that are pending.

An index is built with one embedding identity (provider, model, model files,
dimension), recorded with its first vectors. Vectors of another identity
must never land in it or be compared with it, even when a model cached in a
long-lived process outlives a migration of the index. A write whose vectors
can't be made is still searchable by keyword at once, and its vectors are
filled in later.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

import pytest

import smriti.store.indexer as indexer
from smriti._vendored.memsearch.embeddings.stub import StubEmbedding
from smriti.embed.identity import (
    EmbeddingIdentity,
    identity_of,
    recorded_identity,
)
from smriti.store.indexer import fill_pending_vectors, index_file_result, index_tree
from smriti.store.schema import ensure_schema, open_readonly, pending_vectors
from smriti.store.search import search_with_status

STUB = EmbeddingIdentity("stub", "stub-384", "", 384)


class Shifted:
    """Another model of the same dimension: the stub's vectors rotated, under another name."""

    model_name, dimension = "other-384", 384

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        vectors = await StubEmbedding().embed(texts)
        return [v[1:] + v[:1] for v in vectors]


class Tiny:
    """A model of another dimension."""

    model_name, dimension = "tiny-8", 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0] + [0.0] * 7 for _ in texts]


class Down:
    """An embedder that can't embed (the service is down, the model won't load)."""

    model_name, dimension = "stub-384", 384

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder down")


class Providers:
    """What ``_get_embedding_provider`` returns, switchable mid-test."""

    def __init__(self) -> None:
        self.current: Any = StubEmbedding()
        self.handed: list[Any] = []

    def __call__(self) -> Any:
        self.handed.append(self.current)
        return self.current


@pytest.fixture()
def providers(monkeypatch: pytest.MonkeyPatch) -> Providers:
    p = Providers()
    monkeypatch.setattr(indexer, "_get_embedding_provider", p)
    return p


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "identity.md").write_text("# Identity\n\nAri keeps the lamps lit.\n",
                                          encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "garden.md").write_text(
        "# Garden\n\nSam planted marigolds beside the well.\n", encoding="utf-8")
    db = ensure_schema(tmp_path / ".smriti" / "index.db", 384)
    has_vec = db.has_vec
    db.close()
    if not has_vec:
        pytest.skip("sqlite-vec can't load here; these tests are about vectors")
    return tmp_path


def _db(root: Path) -> Path:
    return root / ".smriti" / "index.db"


def _meta(root: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(_db(root)))
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def _vectors(root: Path, source: str) -> list[list[float]]:
    db = open_readonly(_db(root))
    try:
        out = []
        for (rowid,) in db.execute("SELECT rowid FROM chunks WHERE source = ? ORDER BY rowid",
                                   (source,)).fetchall():
            row = db.execute("SELECT embedding FROM chunks_vec WHERE rowid = ?",
                             (rowid,)).fetchone()
            if row is not None:
                blob = bytes(row[0])
                out.append(list(struct.unpack(f"<{len(blob) // 4}f", blob)))
        return out
    finally:
        db.close()


def _f32(vectors: list[list[float]]) -> list[list[float]]:
    return [list(struct.unpack(f"<{len(v)}f", struct.pack(f"<{len(v)}f", *v))) for v in vectors]


async def _as(provider: Any, texts: list[str]) -> list[list[float]]:
    return _f32(await provider.embed(texts))


def _expected(provider: Any, root: Path, source: str) -> list[list[float]]:
    import asyncio

    db = open_readonly(_db(root))
    try:
        texts = [r[0] for r in db.execute(
            "SELECT content FROM chunks WHERE source = ? ORDER BY rowid", (source,))]
    finally:
        db.close()
    return asyncio.run(_as(provider, texts))


def _recorded(root: Path) -> EmbeddingIdentity | None:
    db = open_readonly(_db(root))
    try:
        return recorded_identity(db)
    finally:
        db.close()


def _vec_loads(tmp: Path) -> bool:
    db = ensure_schema(tmp / "probe.db", None)
    try:
        return db.has_vec
    finally:
        db.close()


def _pending(root: Path) -> int:
    db = open_readonly(_db(root))
    try:
        return pending_vectors(db)
    finally:
        db.close()


def _migrate_on_first_embed(providers: Providers, root: Path, new: Any) -> type:
    """A provider that, while embedding, sees the index migrated to ``new`` (a full reindex)."""

    class MigratedMidEmbed(StubEmbedding):
        fired = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            vectors = await super().embed(texts)
            if not MigratedMidEmbed.fired:
                MigratedMidEmbed.fired = True
                providers.current = new
                worker = threading.Thread(target=lambda: index_tree(full=True, root=root,
                                                                    db=_db(root)))
                worker.start()
                worker.join()
            return vectors

    return MigratedMidEmbed


# ── H10.1 acceptance ─────────────────────────────────────────────────


@pytest.mark.parametrize("model", [Shifted, Tiny], ids=["equal-dimension", "other-dimension"])
def test_a_migration_between_embedding_and_commit_is_caught_by_index_file(
        tree: Path, providers: Providers, model: type) -> None:
    new = model()
    index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "garden.md"
    note.write_text("# Garden\n\nSam moved the marigolds to the gate.\n", encoding="utf-8")
    providers.current = _migrate_on_first_embed(providers, tree, new)()

    result = index_file_result(note, root=tree, db=_db(tree))

    # The stale vectors were thrown away and the file embedded again by the
    # model the index now records; nothing of the old model went in.
    recorded = _recorded(tree)
    assert recorded is not None and recorded.compatible(identity_of(new))
    assert not recorded.compatible(STUB)
    assert result.chunks and not result.vectors_pending
    assert _vectors(tree, "notes/garden.md") == _expected(new, tree, "notes/garden.md")


@pytest.mark.parametrize("model", [Shifted, Tiny], ids=["equal-dimension", "other-dimension"])
def test_a_migration_between_embedding_and_the_search_snapshot_is_caught(
        tree: Path, providers: Providers, model: type) -> None:
    new = model()
    index_tree(root=tree, db=_db(tree))
    providers.current = _migrate_on_first_embed(providers, tree, new)()
    db = open_readonly(_db(tree))
    try:
        outcome = search_with_status(db, "marigolds well", use_reranker=False)
    finally:
        db.close()
    # The first query vector was the old model's; search embedded the query
    # again with the new one rather than compare across models (or fall back).
    assert ["marigolds well"] in new.calls
    assert outcome.keyword_only == ""
    assert outcome.results and outcome.results[0].source == "notes/garden.md"


def test_a_search_whose_embedder_never_matches_runs_keyword_only_and_says_why(
        tree: Path, providers: Providers) -> None:
    index_tree(root=tree, db=_db(tree))
    providers.current = Shifted()
    db = open_readonly(_db(tree))
    try:
        outcome = search_with_status(db, "marigolds well", use_reranker=False)
        keyword = search_with_status(db, "marigolds well", use_reranker=False,
                                     use_vectors=False)
    finally:
        db.close()
    assert "identity mismatch" in outcome.keyword_only
    assert [(r.source, r.score) for r in outcome.results] == [
        (r.source, r.score) for r in keyword.results]
    assert keyword.keyword_only == ""  # not asked for vectors: nothing to report


def test_the_first_write_to_an_empty_index_records_its_identity(
        tmp_path: Path, providers: Providers) -> None:
    note = tmp_path / "notes" / "first.md"
    note.parent.mkdir(parents=True)
    note.write_text("# First\n\nThe very first entry.\n", encoding="utf-8")
    if not _vec_loads(tmp_path):
        pytest.skip("sqlite-vec can't load here")

    assert index_file_result(note, root=tmp_path, db=_db(tmp_path)).chunks
    meta = _meta(tmp_path)
    assert EmbeddingIdentity.from_json(meta["embedding_identity"]) == STUB
    assert (meta["embedding_provider"], meta["model"], meta["dimension"]) == (
        "stub", "stub-384", "384")


def test_the_first_vectors_size_an_index_whose_first_write_was_pending(
        tmp_path: Path, providers: Providers) -> None:
    (tmp_path / "notes").mkdir()
    a, b = tmp_path / "notes" / "a.md", tmp_path / "notes" / "b.md"
    a.write_text("# A\n\nWritten while the embedder was down.\n", encoding="utf-8")
    b.write_text("# B\n\nWritten once it was back.\n", encoding="utf-8")
    if not _vec_loads(tmp_path):
        pytest.skip("sqlite-vec can't load here")
    providers.current = Down()
    assert index_file_result(a, root=tmp_path, db=_db(tmp_path)).vectors_pending
    assert "dimension" not in _meta(tmp_path)  # nothing is recorded without vectors
    providers.current = Tiny()
    assert not index_file_result(b, root=tmp_path, db=_db(tmp_path)).vectors_pending
    assert EmbeddingIdentity.from_json(_meta(tmp_path)["embedding_identity"]) == identity_of(
        Tiny())
    assert _vectors(tmp_path, "notes/a.md") == [[1.0] + [0.0] * 7]  # filled by b's write
    assert _vectors(tmp_path, "notes/b.md") == [[1.0] + [0.0] * 7]
    assert _pending(tmp_path) == 0


def test_a_restart_with_another_provider_installed_refuses_to_mix(
        tree: Path, providers: Providers) -> None:
    index_tree(root=tree, db=_db(tree))
    before = _vectors(tree, "notes/garden.md")
    providers.current = Shifted()  # same dimension: only the identity tells them apart

    with pytest.raises(RuntimeError, match="--full"):
        index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "garden.md"
    note.write_text("# Garden\n\nSam watered the marigolds.\n", encoding="utf-8")
    result = index_file_result(note, root=tree, db=_db(tree))

    assert result.chunks and "doesn't match the index" in result.vectors_pending
    assert _vectors(tree, "notes/garden.md") == []  # pending, not the other model's
    assert _recorded(tree) == STUB
    assert before  # it had the stub's vectors until it changed
    db = open_readonly(_db(tree))
    try:
        outcome = search_with_status(db, "watered marigolds", use_reranker=False)
    finally:
        db.close()
    assert "identity mismatch" in outcome.keyword_only
    assert outcome.results[0].source == "notes/garden.md"  # keyword-searchable at once

    # A deliberate migration replaces everything with the new model.
    assert index_tree(full=True, root=tree, db=_db(tree))["indexed"] == 2
    assert _vectors(tree, "notes/garden.md") == _expected(Shifted(), tree, "notes/garden.md")
    assert _pending(tree) == 0


def test_racing_first_writes_with_different_models_never_mix(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writers give an empty index its first vectors at once, each with its own model."""
    (tmp_path / "notes").mkdir()
    if not _vec_loads(tmp_path):
        pytest.skip("sqlite-vec can't load here")
    files = []
    for name in ("one", "two"):
        f = tmp_path / "notes" / f"{name}.md"
        f.write_text(f"# {name}\n\nEntry {name}.\n", encoding="utf-8")
        files.append(f)
    mine = threading.local()
    monkeypatch.setattr(indexer, "_get_embedding_provider", lambda: mine.provider)
    start = threading.Barrier(2)
    results: list[Any] = [None, None]
    errors: list[BaseException] = []

    def write(i: int) -> None:
        mine.provider = StubEmbedding() if i == 0 else Tiny()
        start.wait()
        try:
            results[i] = index_file_result(files[i], root=tmp_path, db=_db(tmp_path))
        except BaseException as exc:  # noqa: BLE001 — collected, asserted on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    winner = _recorded(tmp_path)
    assert winner is not None and winner in (STUB, identity_of(Tiny()))
    won = 0 if winner == STUB else 1
    assert not results[won].vectors_pending
    assert results[1 - won].vectors_pending and results[1 - won].chunks
    assert all(len(v) == winner.dimension
               for name in ("one", "two") for v in _vectors(tmp_path, f"notes/{name}.md"))
    assert _vectors(tmp_path, f"notes/{('one', 'two')[1 - won]}.md") == []


# ── The schema migration ─────────────────────────────────────────────


def _as_schema_2(db_path: Path) -> None:
    """Turn an index into exactly what the code before H10 left: no vectors column, no record."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        DROP INDEX IF EXISTS idx_sources_pending;
        CREATE TABLE sources_v2 (
            source   TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size     INTEGER NOT NULL
        );
        INSERT INTO sources_v2 SELECT source, mtime_ns, size FROM sources;
        DROP TABLE sources;
        ALTER TABLE sources_v2 RENAME TO sources;
        DELETE FROM meta WHERE key = 'embedding_identity';
        UPDATE meta SET value = '2' WHERE key = 'schema_version';
    """)
    conn.commit()
    conn.close()


def test_a_schema_2_index_migrates_with_every_source_having_vectors(
        tree: Path, providers: Providers) -> None:
    index_tree(root=tree, db=_db(tree))
    _as_schema_2(_db(tree))
    reader = open_readonly(_db(tree))
    try:
        # Readers of an unmigrated index (they never migrate it) see nothing pending,
        # and the old keys stand in for the identity.
        assert pending_vectors(reader) == 0
        assert recorded_identity(reader) == STUB
    finally:
        reader.close()

    conn = ensure_schema(_db(tree), 384)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        assert "vectors" in columns
        assert {r[0] for r in conn.execute("SELECT vectors FROM sources")} == {1}
        # An older writer, still running, records a source the old way: it embedded.
        conn.execute("INSERT OR REPLACE INTO sources (source, mtime_ns, size) VALUES "
                     "('notes/old-writer.md', 1, 1)")
        conn.commit()
        assert conn.execute("SELECT vectors FROM sources WHERE source = "
                            "'notes/old-writer.md'").fetchone()[0] == 1
        conn.execute("DELETE FROM sources WHERE source = 'notes/old-writer.md'")
        conn.commit()
    finally:
        conn.close()
    stats = index_tree(root=tree, db=_db(tree))
    assert (stats["indexed"], stats["skipped"]) == (0, 2)  # nothing re-embedded
    assert EmbeddingIdentity.from_json(_meta(tree)["embedding_identity"]) == STUB


# ── Pending vectors ──────────────────────────────────────────────────


def test_a_write_whose_vectors_cant_be_made_is_keyword_searchable_at_once(
        tree: Path, providers: Providers) -> None:
    index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "lantern.md"
    note.write_text("# Lantern\n\nAri hung a brass lantern by the door.\n", encoding="utf-8")
    providers.current = Down()

    result = index_file_result(note, root=tree, db=_db(tree))

    assert result.chunks == 1 and "embedder down" in result.vectors_pending
    assert _pending(tree) == 1 and _vectors(tree, "notes/lantern.md") == []
    db = open_readonly(_db(tree))
    try:
        outcome = search_with_status(db, "brass lantern", use_reranker=False)
    finally:
        db.close()
    assert outcome.results[0].source == "notes/lantern.md"
    assert "embedder down" in outcome.keyword_only


@pytest.mark.parametrize("repair", ["fill", "index_tree", "next write"])
def test_pending_vectors_are_filled_in(tree: Path, providers: Providers, repair: str) -> None:
    index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "lantern.md"
    note.write_text("# Lantern\n\nAri hung a brass lantern by the door.\n", encoding="utf-8")
    providers.current = Down()
    index_file_result(note, root=tree, db=_db(tree))
    providers.current = StubEmbedding()

    if repair == "fill":
        assert fill_pending_vectors(db=_db(tree)) == 1
    elif repair == "index_tree":
        assert index_tree(root=tree, db=_db(tree))["indexed"] == 1  # unchanged, but pending
    else:
        other = tree / "notes" / "other.md"
        other.write_text("# Other\n\nA later write, embedded.\n", encoding="utf-8")
        assert not index_file_result(other, root=tree, db=_db(tree)).vectors_pending

    assert _pending(tree) == 0
    assert _vectors(tree, "notes/lantern.md") == _expected(StubEmbedding(), tree,
                                                           "notes/lantern.md")
    db = open_readonly(_db(tree))
    try:
        assert search_with_status(db, "brass lantern", use_reranker=False).keyword_only == ""
    finally:
        db.close()


def test_a_source_rewritten_while_its_vectors_were_made_is_left_to_its_writer(
        tree: Path, providers: Providers) -> None:
    index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "lantern.md"
    note.write_text("# Lantern\n\nThe first text.\n", encoding="utf-8")
    providers.current = Down()
    index_file_result(note, root=tree, db=_db(tree))

    def rewrite_then_embed(texts: list[str]) -> Any:
        note.write_text("# Lantern\n\nA second text, written while the repair embedded.\n",
                        encoding="utf-8")
        index_file_result(note, root=tree, db=_db(tree))  # still down: pending again
        from smriti.embed.identity import Embedded
        import asyncio

        return Embedded(asyncio.run(StubEmbedding().embed(texts)), STUB)

    assert fill_pending_vectors(db=_db(tree), embed=rewrite_then_embed) == 0
    assert _pending(tree) == 1 and _vectors(tree, "notes/lantern.md") == []
    providers.current = StubEmbedding()
    assert fill_pending_vectors(db=_db(tree)) == 1
    assert _vectors(tree, "notes/lantern.md") == _expected(StubEmbedding(), tree,
                                                           "notes/lantern.md")


def test_a_repair_with_the_wrong_model_writes_nothing(
        tree: Path, providers: Providers) -> None:
    from smriti.embed.identity import StaleEmbedding

    index_tree(root=tree, db=_db(tree))
    note = tree / "notes" / "lantern.md"
    note.write_text("# Lantern\n\nA lantern.\n", encoding="utf-8")
    providers.current = Down()
    index_file_result(note, root=tree, db=_db(tree))
    providers.current = Shifted()
    with pytest.raises(StaleEmbedding):
        fill_pending_vectors(db=_db(tree))
    assert _pending(tree) == 1 and _vectors(tree, "notes/lantern.md") == []


# ── The identity itself ──────────────────────────────────────────────


def test_unknown_fields_match_anything_known_ones_must_be_equal() -> None:
    a = EmbeddingIdentity("local", "all-MiniLM-L6-v2", "1110a24", 384)
    assert a.compatible(EmbeddingIdentity("local", "all-MiniLM-L6-v2", "", 384))
    assert a.compatible(EmbeddingIdentity("", "all-MiniLM-L6-v2", "", 0))
    assert not a.compatible(EmbeddingIdentity("local", "all-MiniLM-L6-v2", "c9745ed", 384))
    assert not a.compatible(EmbeddingIdentity("onnx", "all-MiniLM-L6-v2", "1110a24", 384))
    assert not a.compatible(EmbeddingIdentity("local", "all-mpnet-base-v2", "", 384))
    assert not a.compatible(EmbeddingIdentity("local", "all-MiniLM-L6-v2", "", 768))
    assert EmbeddingIdentity("local", "m", "", 384).merged(a) == EmbeddingIdentity(
        "local", "m", "1110a24", 384)
    assert EmbeddingIdentity.from_json(a.to_json()) == a
    assert EmbeddingIdentity.from_json("not json") is None


def test_an_older_writer_changing_the_old_keys_wins_over_the_record(tmp_path: Path) -> None:
    """A long-lived process from before H10 keeps writing the old keys; they describe
    the newer vectors, so they win over a record they contradict."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("embedding_identity", STUB.to_json()), ("embedding_provider", "stub"),
        ("model", "stub-384"), ("dimension", "384")])
    assert recorded_identity(conn) == STUB
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
        ("embedding_provider", "onnx"), ("model", "gpahal/bge-m3-onnx-int8"),
        ("dimension", "1024")])
    assert recorded_identity(conn) == EmbeddingIdentity("onnx", "gpahal/bge-m3-onnx-int8", "",
                                                        1024)
    conn.close()


def test_a_local_providers_revision_is_its_hub_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = pytest.importorskip("huggingface_hub")
    from smriti.embed import identity

    snap = tmp_path / "models--sentence-transformers--all-MiniLM-L6-v2" / "snapshots" / "abc123"

    def cached(repo: str, filename: str) -> str:
        assert repo == "sentence-transformers/all-MiniLM-L6-v2"
        return str(snap / filename)

    monkeypatch.setattr(hub, "try_to_load_from_cache", cached)
    assert identity.model_revision("local", "all-MiniLM-L6-v2") == "abc123"
    assert identity.model_revision("stub", "stub-384") == ""


"""Embedding-provider selection: one rule (``smriti.embed.provider.provider_keys``).

Before H12.5 the indexer let ``SMRITI_EMBED_PROVIDER`` win over the provider
the index records, and the embedding service let the record win (outside
review #5, Opus problem 4). Both now ask ``provider_keys``: the recorded
provider for everything that serves the index, the override only for a new
or empty index or a rebuild (``smriti index --full``).
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
from pathlib import Path

import pytest

from smriti._vendored.memsearch.embeddings import DEFAULT_MODELS
from smriti.core.tree import smriti_db_path, tree_root
from smriti.embed import provider as embedding
from smriti.embed.identity import provider_for_model
from smriti.embed.provider import recorded_provider


def _legacy_index(path: Path, model: str) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('model', ?)", (model,))
    conn.execute("INSERT INTO meta VALUES ('dimension', '384')")
    conn.commit()
    conn.close()
    return path


def test_legacy_index_without_provider_key_infers_from_model(tmp_path: Path) -> None:
    """Indexes from before e8d98c2 record model + dimension but no
    embedding_provider; the auto order would pick ONNX and strand a
    MiniLM index behind the dimension guard (review)."""
    db = _legacy_index(tmp_path / "index.db", "all-MiniLM-L6-v2")
    assert recorded_provider(db) == "local"


def test_recorded_provider_key_wins_over_inference(tmp_path: Path) -> None:
    db = _legacy_index(tmp_path / "index.db", "all-MiniLM-L6-v2")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO meta VALUES ('embedding_provider', 'onnx')")
    conn.commit()
    conn.close()
    assert recorded_provider(db) == "onnx"


def test_provider_for_model_names() -> None:
    assert provider_for_model("BAAI/bge-m3") == "onnx"
    assert provider_for_model("all-MiniLM-L6-v2") == "local"
    assert provider_for_model("something-else") is None


def test_missing_index_has_no_recorded_provider(tmp_path: Path) -> None:
    assert recorded_provider(tmp_path / "nope.db") is None


# ── The one rule, and both of its callers ────────────────────────────


class Fake:
    """A provider as the registry key ``key`` would load it: its model names the key."""

    revision = ""  # known: no hub lookup

    def __init__(self, key: str) -> None:
        self.model_name = DEFAULT_MODELS[key]
        self.dimension = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 7 for _ in texts]


@pytest.fixture()
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The registry keys loaded, in order; a fresh in-process cache, restored after."""
    calls: list[str] = []

    def get_provider(name: str, **_: object) -> Fake:
        calls.append(name)
        return Fake(name)

    monkeypatch.setattr("smriti._vendored.memsearch.embeddings.get_provider", get_provider)
    monkeypatch.setattr(embedding, "_loaded", None)
    monkeypatch.setattr(embedding, "_overrides_noted", set())  # each test hears the warning
    monkeypatch.delenv("SMRITI_EMBED_PROVIDER", raising=False)
    return calls


def _record(db: Path, key: str | None) -> None:
    if key is None:
        if db.exists():
            db.unlink()
        return
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embedding_provider', ?)", (key,))
    conn.commit()
    conn.close()


def _expected(recorded: str | None, override: str, rebuild: bool) -> tuple[str, ...]:
    if rebuild and override:
        return (override,)
    if recorded:
        return (recorded,)
    return (override,) if override else embedding.AUTO_ORDER


@pytest.mark.parametrize(("recorded", "override"), list(itertools.product(
    [None, "local", "onnx"], ["", "local", "onnx", "stub"])))
def test_the_service_and_a_process_serving_the_index_follow_one_rule(
        asked: list[str], monkeypatch: pytest.MonkeyPatch,
        recorded: str | None, override: str) -> None:
    from smriti.embed.service import _load_provider

    db = smriti_db_path()
    _record(db, recorded)
    if override:
        monkeypatch.setenv("SMRITI_EMBED_PROVIDER", override)
    want = _expected(recorded, override, rebuild=False)
    assert embedding.provider_keys(db) == want
    assert embedding.provider_keys(db, rebuild=True) == _expected(recorded, override, True)

    _load_provider(db)  # the service
    served = asked[-1]
    embedding.local_provider()  # a search or write with the service off
    assert served == asked[-1] == want[0]
    with embedding.in_process_embeddings(db=db):  # an incremental index run
        assert embedding.local_provider().model_name == DEFAULT_MODELS[want[0]]
    assert len(asked) == 2  # one model kept: an incremental run loads nothing new


def test_only_a_rebuild_migrates_to_the_override(
        asked: list[str], monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """`SMRITI_EMBED_PROVIDER=X smriti index --full` migrates the index to X, as
    before H12.5. An incremental run, a search, a write and the service keep the
    recorded model: before, the indexer's override made an incremental run
    refuse ("run 'smriti index --full'") and an in-process search keyword-only."""
    from smriti.embed.identity import recorded_identity
    from smriti.embed.service import _load_provider
    from smriti.store.indexer import index_file_result, index_tree
    from smriti.store.schema import open_readonly

    root, db = tree_root(), smriti_db_path()
    (root / "notes").mkdir(exist_ok=True)
    (root / "notes" / "lamp.md").write_text("# Lamp\n\nAri trims the wick.\n",
                                            encoding="utf-8")

    def recorded() -> str:
        conn = open_readonly(db)
        try:
            identity = recorded_identity(conn)
        finally:
            conn.close()
        assert identity is not None
        return identity.provider

    monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "local")
    index_tree(root=root, db=db)  # an empty index: the override names its model
    assert asked == ["local"] and recorded() == "local"

    monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "onnx")
    embedding.forget_provider()  # a new process
    (root / "notes" / "well.md").write_text("# Well\n\nSam draws water.\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="smriti.embed.provider"):
        assert index_tree(root=root, db=db)["indexed"] == 1
    assert asked == ["local", "local"] and recorded() == "local"
    assert "SMRITI_EMBED_PROVIDER=onnx is not used" in caplog.text
    (root / "notes" / "well.md").write_text("# Well\n\nSam draws more.\n", encoding="utf-8")
    assert not index_file_result(root / "notes" / "well.md", root=root, db=db).vectors_pending
    _load_provider(db)
    assert asked == ["local", "local", "local"]

    stats = index_tree(full=True, root=root, db=db)  # the migration
    assert asked[-1] == "onnx" and recorded() == "onnx" and stats["indexed"] == 2
    monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "local")
    _load_provider(db)
    assert asked[-1] == "onnx"  # the service follows the index it serves

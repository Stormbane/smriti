"""H15.9: smriti's default models load at a pinned commit, and run no repository code.

A model fetched from the Hugging Face Hub by its name alone is whatever the
repository's ``main`` holds: an upstream change, or a compromised repository,
would change what runs and what the index's vectors are. The default models
(the onnx and local embedding providers' and the reranker's) load at the
commit in ``smriti._vendored.memsearch.revisions``, from the cache and from
the Hub alike, and the sentence-transformers paths don't trust a repository's
code. The identity an index records names that commit's snapshot, so pinning
the commit the cache already held changes no index.

The Hub's functions and the model runtimes are faked: no network, no model.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from smriti._vendored.memsearch.embeddings import DEFAULT_MODELS
from smriti._vendored.memsearch.embeddings.onnx import OnnxEmbedding
from smriti._vendored.memsearch.revisions import PINNED_REVISIONS, pinned_revision
from smriti.embed import identity
from smriti.embed.identity import EmbeddingIdentity, identity_of

ONNX_MODEL = "gpahal/bge-m3-onnx-int8"
PIN = "2b34e84df040034d4b9eabb62383a87c18955822"
NEWER = "f" * 40  # a commit main moved to after the pin
RERANKER = "Alibaba-NLP/gte-reranker-modernbert-base"


class Hub:
    """hf_hub_download and list_repo_files, recording each call; *cached* is in the cache."""

    def __init__(self, cached: set[str]) -> None:
        self.cached = cached
        self.calls: list[tuple[str, str, str | None, bool]] = []  # repo, file, revision, offline

    def download(self, repo: str, filename: str, *, revision: str | None = None,
                 local_files_only: bool = False) -> str:
        self.calls.append((repo, filename, revision, local_files_only))
        if local_files_only and filename not in self.cached:
            raise FileNotFoundError(filename)
        return f"/hub/{repo}/snapshots/{revision or 'main'}/{filename}"

    def list_files(self, repo: str, *, revision: str | None = None) -> list[str]:
        self.calls.append((repo, "<list>", revision, False))
        return ["tokenizer.json", "model_quantized.onnx", "README.md"]


def test_every_default_model_is_pinned_to_a_commit() -> None:
    from smriti._vendored.memsearch.reranker import DEFAULT_RERANKER

    for model in (DEFAULT_MODELS["onnx"], DEFAULT_MODELS["local"], DEFAULT_RERANKER):
        pin = pinned_revision(model)
        assert pin is not None and len(pin) == 40, model
        assert all(c in "0123456789abcdef" for c in pin), model
    assert set(PINNED_REVISIONS) == {DEFAULT_MODELS["onnx"], DEFAULT_MODELS["local"],
                                     DEFAULT_RERANKER}
    assert pinned_revision("someone/their-own-model") is None


def test_the_default_onnx_model_is_pinned_to_the_commit_indexes_record() -> None:
    # Every index made with the default model before the pin records this
    # snapshot (the Hub's main since 2025-06-25). Moving the pin changes their
    # identity, and each would have to be embedded again.
    assert pinned_revision(ONNX_MODEL) == PIN


def test_the_default_onnx_model_loads_from_the_cache_at_its_pin() -> None:
    hub = Hub({"tokenizer.json", "model_quantized.onnx"})
    tok, model = OnnxEmbedding._download_model_files(ONNX_MODEL, hub.download, hub.list_files)
    assert (tok, model) == (f"/hub/{ONNX_MODEL}/snapshots/{PIN}/tokenizer.json",
                            f"/hub/{ONNX_MODEL}/snapshots/{PIN}/model_quantized.onnx")
    assert hub.calls  # all from the cache: no listing, nothing online
    assert all(rev == PIN and offline for _, _, rev, offline in hub.calls), hub.calls


def test_the_default_onnx_model_downloads_at_its_pin() -> None:
    hub = Hub(set())  # nothing cached: the online path
    tok, model = OnnxEmbedding._download_model_files(ONNX_MODEL, hub.download, hub.list_files)
    assert (tok, model) == (f"/hub/{ONNX_MODEL}/snapshots/{PIN}/tokenizer.json",
                            f"/hub/{ONNX_MODEL}/snapshots/{PIN}/model_quantized.onnx")
    online = [c for c in hub.calls if not c[3]]
    assert {f for _, f, _, _ in online} == {"tokenizer.json", "<list>", "model_quantized.onnx"}
    assert all(rev == PIN for _, _, rev, _ in hub.calls), hub.calls


def test_a_model_with_no_pin_loads_at_main() -> None:
    for cached in ({"tokenizer.json", "model.onnx"}, set()):
        hub = Hub(cached)
        OnnxEmbedding._download_model_files("someone/their-onnx", hub.download, hub.list_files)
        assert all(rev is None for _, _, rev, _ in hub.calls), hub.calls


# ── The cache's own lookups, on a cache laid out as the Hub lays it out ──


@pytest.fixture
def hub_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    """A Hub cache in tmp_path holding the default onnx model at PIN and at NEWER.

    Returns a function that points the cache's ``main`` at a commit.
    """
    constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True)
    repo = tmp_path / "models--gpahal--bge-m3-onnx-int8"
    for commit in (PIN, NEWER):
        snap = repo / "snapshots" / commit
        snap.mkdir(parents=True)
        (snap / "tokenizer.json").write_text("{}", encoding="utf-8")
        (snap / "model_quantized.onnx").write_bytes(b"onnx")
    (repo / "refs").mkdir()

    def main_at(commit: str) -> Path:
        (repo / "refs" / "main").write_text(commit, encoding="utf-8")
        return repo

    return main_at


def _offline(*args: Any, **kwargs: Any) -> list[str]:
    raise AssertionError("the cache held the model: nothing is asked of the Hub")


def test_a_pinned_load_reads_the_pinned_snapshot_whatever_main_names(
        hub_cache: Callable[[str], Path]) -> None:
    from huggingface_hub import hf_hub_download

    repo = hub_cache(NEWER)
    tok, model = OnnxEmbedding._download_model_files(ONNX_MODEL, hf_hub_download, _offline)
    assert Path(tok) == repo / "snapshots" / PIN / "tokenizer.json"
    assert Path(model) == repo / "snapshots" / PIN / "model_quantized.onnx"


def _loaded_onnx(dimension: int = 1024) -> OnnxEmbedding:
    """An OnnxEmbedding as a load leaves it, without loading a model."""
    provider = object.__new__(OnnxEmbedding)
    provider._model = ONNX_MODEL
    provider._dimension = dimension
    return provider


def test_a_pinned_loads_identity_names_the_pinned_snapshot(
        hub_cache: Callable[[str], Path]) -> None:
    hub_cache(NEWER)
    assert identity.model_revision("onnx", ONNX_MODEL) == PIN
    assert identity_of(_loaded_onnx()) == EmbeddingIdentity("onnx", ONNX_MODEL, PIN, 1024)


def test_where_main_is_the_pin_the_identity_is_what_it_was_before_the_pin(
        hub_cache: Callable[[str], Path]) -> None:
    # The cache as an install that downloaded the model before the pin holds
    # it: main names the commit the pin now names. The snapshot the unpinned
    # rule found there (main's) is the pinned one, so the index's recorded
    # identity still matches and nothing is embedded again.
    from huggingface_hub import try_to_load_from_cache

    hub_cache(PIN)
    before = Path(try_to_load_from_cache(ONNX_MODEL, "tokenizer.json")).parent.name
    assert before == PIN
    assert identity_of(_loaded_onnx()) == EmbeddingIdentity("onnx", ONNX_MODEL, before, 1024)


# ── sentence-transformers: the local provider and the reranker's torch backend ──


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


class _Recorded:
    """A sentence-transformers model class that records how it was made."""

    made: list[tuple[str, dict[str, Any]]]

    def __init__(self, model: str, **kwargs: Any) -> None:
        type(self).made.append((model, kwargs))

    def get_sentence_embedding_dimension(self) -> int:
        return 384


@pytest.fixture
def sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """A fake sentence_transformers module, recording each model made."""
    made: list[tuple[str, dict[str, Any]]] = []
    st = _module("sentence_transformers",
                 SentenceTransformer=type("SentenceTransformer", (_Recorded,), {"made": made}),
                 CrossEncoder=type("CrossEncoder", (_Recorded,), {"made": made}))
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    return types.SimpleNamespace(made=made)


def test_the_local_provider_loads_at_its_pin_and_trusts_no_repository_code(
        sentence_transformers: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti._vendored.memsearch.embeddings import local

    monkeypatch.setattr(local, "_detect_device", lambda: "cpu")
    local.LocalEmbedding()
    local.LocalEmbedding("someone/their-own-model")
    (default, default_kw), (named, named_kw) = sentence_transformers.made
    assert default == DEFAULT_MODELS["local"]
    assert default_kw["revision"] == PINNED_REVISIONS[DEFAULT_MODELS["local"]]
    assert named == "someone/their-own-model" and named_kw["revision"] is None
    assert default_kw["trust_remote_code"] is False and named_kw["trust_remote_code"] is False


def test_the_torch_reranker_loads_at_its_pin(
        sentence_transformers: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    from smriti._vendored.memsearch import reranker

    monkeypatch.setattr(reranker, "_torch_cache", {})
    reranker._load_torch_model(RERANKER)
    [(model, kwargs)] = sentence_transformers.made
    assert model == RERANKER and kwargs["revision"] == PINNED_REVISIONS[RERANKER]
    assert not kwargs.get("trust_remote_code", False)


def test_the_onnx_reranker_loads_at_its_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    from smriti._vendored.memsearch import reranker

    hub = Hub(set())
    tokenizer = types.SimpleNamespace(enable_truncation=lambda **kw: None,
                                      no_padding=lambda: None)
    session = types.SimpleNamespace(get_inputs=lambda: [])
    fakes = [
        _module("huggingface_hub", hf_hub_download=hub.download,
                list_repo_files=hub.list_files),
        _module("tokenizers", Tokenizer=types.SimpleNamespace(from_file=lambda p: tokenizer)),
        _module("onnxruntime", InferenceSession=lambda p: session),
    ]
    for module in fakes:
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(reranker, "_onnx_cache", {})

    reranker._load_onnx_model(RERANKER)
    assert {f for _, f, _, _ in hub.calls} == {"<list>", "onnx/model_quantized.onnx",
                                               "tokenizer.json"}
    assert all(rev == PINNED_REVISIONS[RERANKER] for _, _, rev, _ in hub.calls), hub.calls

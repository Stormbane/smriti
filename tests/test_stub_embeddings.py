"""The CI embedder must work without optional dependencies or model downloads."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from smriti._vendored.memsearch.embeddings import DEFAULT_MODELS, EmbeddingProvider, get_provider


def test_stub_interface_and_empty_inputs() -> None:
    provider = get_provider("stub", batch_size=2)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.model_name == DEFAULT_MODELS["stub"] == "stub-384"
    assert provider.dimension == 384
    assert asyncio.run(provider.embed([])) == []
    assert asyncio.run(provider.embed(["", " ! "])) == [[0.0] * 384] * 2
    assert get_provider("stub", model="test-name").model_name == "test-name"


def test_stub_word_counts_and_normalisation() -> None:
    vectors = asyncio.run(get_provider("stub").embed([
        "Apple banana apple", "BANANA, apple APPLE!", "apple", "banana", "carrot",
    ]))
    assert vectors[0] == vectors[1]
    assert vectors[2] != vectors[3] != vectors[4]
    assert sorted(value for value in vectors[0] if value) == pytest.approx([
        1 / math.sqrt(5), 2 / math.sqrt(5),
    ])
    for vector in vectors:
        assert len(vector) == 384
        assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1)


def test_stub_is_stable_without_site_packages(tmp_path: Path) -> None:
    # -S excludes numpy and every other site package. Different hash seeds
    # prove Python's process-randomised hash() cannot affect the vectors.
    script = (
        "import asyncio, json; "
        "from smriti._vendored.memsearch.embeddings import get_provider; "
        "print(json.dumps(asyncio.run(get_provider('stub').embed(['Apple apple caf\u00e9']))))"
    )
    outputs = []
    for seed in ("1", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run([sys.executable, "-S", "-c", script], env=env,
                                cwd=tmp_path, capture_output=True, text=True, check=True,
                                timeout=30)
        outputs.append(json.loads(result.stdout))
    expected = asyncio.run(get_provider("stub").embed(["Apple apple caf\u00e9"]))
    assert outputs == [expected, expected]


def test_environment_selects_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from smriti.store import indexer

    monkeypatch.setenv("SMRITI_EMBED_PROVIDER", "stub")
    # Bypass the cache without disturbing the provider used by other tests.
    provider = indexer._get_embedding_provider.__wrapped__()
    assert provider.model_name == "stub-384"
    assert provider.dimension == 384

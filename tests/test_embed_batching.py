"""H15.10: one long text can't pad a whole embedding batch.

The onnx provider embedded 32 texts per model call, each padded to the
longest, so one text of 6,000 tokens made the call compute attention for
32 texts of 6,000 tokens: tens of gigabytes (LongMemEval,
``docs/BENCHMARKS.md``). ``batched_embed`` now takes a length and a bound on
a call's attention (its size times the square of its longest text): a batch
over it goes in runs of texts of like length. A batch within it is the same
call as before, and the results are in input order either way.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from smriti._vendored.memsearch.embeddings.onnx import OnnxEmbedding
from smriti._vendored.memsearch.embeddings.utils import batched_embed


class Recorder:
    """An embed_fn that records its batches; a text's vector names the text."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[float(len(t)), float(sum(map(ord, t)))] for t in texts]


def _vector(text: str) -> list[float]:
    return [float(len(text)), float(sum(map(ord, text)))]


def _embed(texts: list[str], batch_size: int, max_attention: int = 0,
           measured: bool = True) -> tuple[list[list[float]], Recorder]:
    recorder = Recorder()
    if measured:
        got = asyncio.run(batched_embed(texts, recorder, batch_size, length=len,
                                        max_attention=max_attention))
    else:
        got = asyncio.run(batched_embed(texts, recorder, batch_size))
    return got, recorder


def test_a_long_text_is_embedded_apart_and_the_results_keep_their_order() -> None:
    texts = [f"short {i}" for i in range(7)]
    texts.insert(3, "L" * 100)
    got, recorder = _embed(texts, batch_size=8, max_attention=8 * 10 * 10)
    assert got == [_vector(t) for t in texts]
    assert recorder.batches == [[t for t in texts if len(t) < 100], ["L" * 100]]


def test_a_single_text_is_embedded_without_being_measured() -> None:
    # A query, or a write of one chunk: nothing for it to pad, so its length
    # (a tokenizer pass on the real provider) isn't worth taking.
    measured: list[str] = []

    def length(text: str) -> int:
        measured.append(text)
        return len(text)

    recorder = Recorder()
    got = asyncio.run(batched_embed(["L" * 5000], recorder, 32, length=length,
                                    max_attention=8 * 10 * 10))
    assert got == [_vector("L" * 5000)] and recorder.batches == [["L" * 5000]]
    assert measured == []


def test_a_batch_over_the_bound_goes_in_runs_of_like_length() -> None:
    rng = random.Random(10)
    texts = [f"{i:03d}" + "x" * rng.choice([rng.randint(1, 20), rng.randint(20, 200), 2000])
             for i in range(100)]
    bound = 16 * 40 * 40
    got, recorder = _embed(texts, batch_size=16, max_attention=bound)
    assert got == [_vector(t) for t in texts]
    assert sorted(map(len, (t for b in recorder.batches for t in b))) == sorted(map(len, texts))
    for batch in recorder.batches:
        assert len(batch) <= 16
        assert len(batch) == 1 or len(batch) * max(map(len, batch)) ** 2 <= bound
    # Within each window of 16 inputs, the runs go shortest first: each run's
    # texts are no longer than the next run's.
    seen = 0
    for start in range(0, len(texts), 16):
        window = texts[start:start + 16]
        runs = []
        while sum(map(len, runs)) < len(window):
            runs.append(recorder.batches[seen])
            seen += 1
        assert sorted(t for r in runs for t in r) == sorted(window)
        for a, b in zip(runs, runs[1:], strict=False):
            assert max(map(len, a)) <= min(map(len, b))
    assert seen == len(recorder.batches)


def test_a_batch_within_the_bound_is_the_same_call_as_before() -> None:
    texts = [f"text number {i}" for i in range(70)]
    got, recorder = _embed(texts, batch_size=32, max_attention=32 * 20 * 20)
    before, old = _embed(texts, batch_size=32, measured=False)
    assert recorder.batches == old.batches == [texts[:32], texts[32:64], texts[64:]]
    assert got == before == [_vector(t) for t in texts]


class _Encoding:
    def __init__(self, n: int) -> None:
        self.ids = list(range(n))


class _Tokenizer:
    """One token per word, truncated at 8,192 as the provider's tokenizer is."""

    def encode(self, text: str) -> _Encoding:
        return _Encoding(min(len(text.split()) + 2, 8192))


def _onnx(batches: list[list[str]]) -> OnnxEmbedding:
    """The onnx provider without its model: _encode records each call."""
    provider = OnnxEmbedding.__new__(OnnxEmbedding)
    any_provider: Any = provider
    any_provider._tokenizer = _Tokenizer()
    any_provider._batch_size = OnnxEmbedding._DEFAULT_BATCH_SIZE

    def encode(texts: list[str]) -> list[list[float]]:
        batches.append(list(texts))
        return [_vector(t) for t in texts]

    any_provider._encode = encode
    return provider


def test_the_onnx_provider_keeps_a_long_text_out_of_a_batch_of_short_ones() -> None:
    batches: list[list[str]] = []
    provider = _onnx(batches)
    short = [f"Ari and Sam talked about the harbour, note {i}." for i in range(31)]
    long = " ".join(["word"] * 6_000)  # about 6,000 tokens: one LongMemEval turn
    texts = short[:10] + [long] + short[10:]
    got = asyncio.run(provider.embed(texts))
    assert got == [_vector(t) for t in texts]
    assert batches == [short, [long]]
    # A batch of ordinary chunks is one call, as before.
    batches.clear()
    asyncio.run(provider.embed(short + short[:1]))
    assert batches == [short + short[:1]]

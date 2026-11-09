"""Shared utilities for embedding providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable


async def batched_embed(
    texts: list[str],
    embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    batch_size: int,
    *,
    length: Callable[[str], int] | None = None,
    max_attention: int = 0,
) -> list[list[float]]:
    """Split *texts* into batches and call *embed_fn* on each.

    Parameters
    ----------
    texts:
        The texts to embed.
    embed_fn:
        An async callable that embeds a single batch of texts.
    batch_size:
        Maximum number of texts per batch.  Must be >= 1.
    length, max_attention:
        smriti (upstream has neither): for a model that pads every text of a
        batch to its longest, the length of a text (in tokens) and the most
        attention one call may compute, counted as the batch's size times
        the square of its longest text.  A batch over it is split: its texts
        are taken shortest first, in runs that stay within it (a text over
        it alone), so one long text can't pad all the others.  Batches are
        still the runs of *batch_size* texts in input order, so a batch
        within the bound is embedded as upstream embeds it.  The results
        are in input order either way.
    """
    if not texts:
        return []
    if batch_size <= 0:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if len(texts) <= batch_size and length is None:
        return await embed_fn(texts)
    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        if length is None:
            results.extend(await embed_fn(batch))
        else:
            results.extend(await _embed_within(batch, embed_fn, length, max_attention))
    return results


async def _embed_within(
    batch: list[str],
    embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    length: Callable[[str], int],
    max_attention: int,
) -> list[list[float]]:
    """smriti: embed one batch in one call, or, over *max_attention*, in runs by length."""
    lengths = [length(t) for t in batch]
    if len(batch) * max(lengths) ** 2 <= max_attention:
        return await embed_fn(batch)
    out: list[list[float]] = [[] for _ in batch]
    run: list[int] = []

    async def flush() -> None:
        vectors = await embed_fn([batch[j] for j in run])
        for j, vector in zip(run, vectors, strict=True):
            out[j] = vector
        run.clear()

    # Sorted (stably) by length, so a run's longest text is its last.
    for i in sorted(range(len(batch)), key=lengths.__getitem__):
        if run and (len(run) + 1) * lengths[i] ** 2 > max_attention:
            await flush()
        run.append(i)
    await flush()
    return out

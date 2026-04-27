"""Stage 3: cross-cluster thread synthesis.

After a consolidate drain produces N new or revised concept pages, one
synthesis call looks across all of them and names the threads running
through the day's ingested material. Output goes to
`semantic/threads/{date}-threads.md` -- itself a write, so the existing
cognitive cascade will propagate any upstream implications.

Input: list of concept pages that changed this drain (from
`batch_consolidate` `ClusterResult.concept_page`).

Output: a single dated threads page, or None if nothing warranted
synthesis (< 2 changed concepts).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from smriti.store.judge import summarize_via_claude

log = logging.getLogger(__name__)

_MAX_CONCEPT_EXCERPT = 3000   # chars per concept fed to synthesis
_MIN_CONCEPTS_FOR_SYNTHESIS = 2
# Above this count the synthesis prompt collapses -- claude -p starts
# returning meta-comments ("permission to persist...") instead of synthesis.
# Empirically, 12 worked, 27 collapsed. Chunk above the cap into multiple
# parts.
_MAX_CONCEPTS_PER_THREADS = int(os.environ.get("NARADA_THREADS_MAX_CONCEPTS", "15"))


_THREADS_PROMPT = """\
The following {n} concept pages were created or revised in a single
consolidation pass. Your job is to find the threads running across them:
patterns, tensions, recurring questions, or unexpected connections that
are visible only in the aggregate.

Produce 2-5 named threads. For each thread:

- **Title** -- one line, specific
- **Claim** -- the load-bearing assertion this thread carries
- **Contributing concepts** -- which of the N pages contribute, by filename
- **Open questions** -- anything the aggregate leaves unresolved

Avoid repeating what any single concept already says. The value of this
page is what emerges only when several concepts are read together. If
fewer than 2 genuine threads are visible, say so -- do not manufacture
threads to fill the form.

No preamble, no meta-commentary. Start with `# Threads ({date})`.

--- CONCEPTS ---

{concepts}
"""


@dataclass
class ThreadsResult:
    threads_path: Path | None
    concept_count: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0
    skipped_reason: str = ""


def synthesize_threads(
    concept_paths: list[Path],
    root: Path,
    *,
    executor_fn: Callable[[str], tuple[str, object]] = summarize_via_claude,
    today: str | None = None,
) -> ThreadsResult:
    """Run synthesis across the given concept pages.

    For batches above _MAX_CONCEPTS_PER_THREADS (default 15), the synthesis
    prompt collapses (claude -p returns meta-comment instead of synthesis).
    For larger inputs, use ``synthesize_threads_chunked`` which produces
    multiple threads docs.

    This single-call entrypoint stays for callers that have already chosen
    a small batch. Returns the first chunk's result if input is large
    (callers preferring multi-doc behavior should switch to
    ``synthesize_threads_chunked``).
    """
    if today is None:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")

    results = synthesize_threads_chunked(
        concept_paths, root, executor_fn=executor_fn, today=today,
    )
    return results[0] if results else ThreadsResult(
        threads_path=None, concept_count=0, skipped_reason="no input",
    )


def synthesize_threads_chunked(
    concept_paths: list[Path],
    root: Path,
    *,
    executor_fn: Callable[[str], tuple[str, object]] = summarize_via_claude,
    today: str | None = None,
    max_per_chunk: int | None = None,
) -> list[ThreadsResult]:
    """Synthesize threads, chunking inputs above the per-call cap.

    Above _MAX_CONCEPTS_PER_THREADS, splits into chunks and produces one
    threads doc per chunk with `-part-N` suffix. Returns one ThreadsResult
    per chunk processed. Empty list if nothing to synthesize.

    Caller (e.g. sleep dispatcher) is expected to run Stage 4 actionables
    on each returned threads_path independently.
    """
    # Filename always includes a time-of-day stamp so multiple drains in a
    # single day do NOT clobber each other. Previous bug: Stage B in the
    # morning and Stage C in the evening both wrote `2026-04-24-threads.md`,
    # silently overwriting the morning's synthesis. Use UTC HHMM as suffix.
    now = datetime.now(timezone.utc)
    if today is None:
        today = now.strftime("%Y-%m-%d")
    time_stamp = now.strftime("%H%M")
    if max_per_chunk is None:
        max_per_chunk = _MAX_CONCEPTS_PER_THREADS

    # Dedup + filter to existing
    unique: list[Path] = []
    seen: set[Path] = set()
    for p in concept_paths:
        if p is None or p in seen or not p.exists():
            continue
        seen.add(p)
        unique.append(p)

    if len(unique) < _MIN_CONCEPTS_FOR_SYNTHESIS:
        return [ThreadsResult(
            threads_path=None,
            concept_count=len(unique),
            skipped_reason=f"only {len(unique)} concept(s), need >= {_MIN_CONCEPTS_FOR_SYNTHESIS}",
        )]

    # Chunk
    chunks: list[list[Path]] = [
        unique[i:i + max_per_chunk]
        for i in range(0, len(unique), max_per_chunk)
    ]
    multi = len(chunks) > 1

    log.info(
        "Threads synthesis: %d concepts in %d chunk(s) of <=%d",
        len(unique), len(chunks), max_per_chunk,
    )

    results: list[ThreadsResult] = []
    for idx, chunk in enumerate(chunks, start=1):
        # Filename: {YYYY-MM-DD}-{HHMM}[-part-N]-threads.md
        # Time-of-day prevents same-day collisions; part suffix
        # disambiguates chunks from a single run.
        part_suffix = f"-part-{idx}" if multi else ""
        chunk_label = f"{today}-{time_stamp}{part_suffix}"
        result = _synthesize_one_chunk(
            chunk, root, executor_fn=executor_fn, today_label=chunk_label,
        )
        results.append(result)
    return results


def _synthesize_one_chunk(
    concepts: list[Path],
    root: Path,
    *,
    executor_fn: Callable[[str], tuple[str, object]],
    today_label: str,
) -> ThreadsResult:
    """Inner: write a single threads doc from <=_MAX_CONCEPTS_PER_THREADS concepts."""
    concept_blocks: list[str] = []
    for p in concepts:
        try:
            content = p.read_text(encoding="utf-8")[:_MAX_CONCEPT_EXCERPT]
        except OSError:
            continue
        rel = str(p.relative_to(root)).replace("\\", "/") if p.is_relative_to(root) else str(p)
        concept_blocks.append(f"## {rel}\n\n{content}")
    concepts_text = "\n\n---\n\n".join(concept_blocks)

    prompt = _THREADS_PROMPT.format(
        n=len(concepts),
        date=today_label,
        concepts=concepts_text,
    )

    t0 = time.monotonic()
    try:
        text, meta = executor_fn(prompt)
    except Exception as exc:
        log.warning("threads synthesis failed: %s", exc)
        return ThreadsResult(
            threads_path=None,
            concept_count=len(concepts),
            skipped_reason=f"executor failed: {exc}",
        )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    threads_dir = root / "semantic" / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    out_path = threads_dir / f"{today_label}-threads.md"

    header = (
        f"---\n"
        f"type: threads-synthesis\n"
        f"date: {today_label}\n"
        f"concept_count: {len(concepts)}\n"
        f"sources:\n"
    )
    for p in concepts:
        rel = str(p.relative_to(root)).replace("\\", "/") if p.is_relative_to(root) else str(p)
        header += f"  - {rel}\n"
    header += "---\n\n"

    out_path.write_text(header + text.strip() + "\n", encoding="utf-8")

    log.info(
        "Synthesized threads from %d concepts -> %s (%d in, %d out, $%.4f, %dms)",
        len(concepts), out_path.name,
        getattr(meta, "tokens_in", 0), getattr(meta, "tokens_out", 0),
        getattr(meta, "cost_usd", 0.0), elapsed_ms,
    )

    return ThreadsResult(
        threads_path=out_path,
        concept_count=len(concepts),
        tokens_in=getattr(meta, "tokens_in", 0),
        tokens_out=getattr(meta, "tokens_out", 0),
        cost_usd=getattr(meta, "cost_usd", 0.0),
        elapsed_ms=elapsed_ms,
    )

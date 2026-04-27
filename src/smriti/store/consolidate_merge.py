"""Destructive concept-page merge to reduce concept-wiki sprawl.

When the consolidate pipeline creates too many near-duplicate concept pages
(sovereignty.md + sovereign-trust.md + bilateral-sovereignty.md all as
distinct files for facets of the same idea), this pass finds them and
merges destructively:

1. Compute pairwise cosine similarity across all semantic/concepts/*.md
2. For pairs above MERGE_THRESHOLD, pick a winner (more sources / longer /
   existing) and a loser, call executor to merge, delete the loser,
   rewrite wikilinks pointing at the loser to point at the winner.
3. Queue cognitive cascade from the winner.

Destructive by design -- this is the only place in smriti that deletes
content files. Run explicitly via CLI, never auto-scheduled without
--dry-run first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from smriti.core.tree import tree_root
from smriti.store.cascade import PROTECTED_FILES, queue_cognitive_cascade
from smriti.store.consolidate import _embed_texts
from smriti.store.judge import executor_via_claude

log = logging.getLogger(__name__)


_MERGE_THRESHOLD = float(os.environ.get("NARADA_CONCEPT_MERGE_THRESHOLD", "0.85"))
_MAX_MERGE_CONTENT = 30_000


@dataclass
class MergePair:
    winner: Path
    loser: Path
    similarity: float
    winner_sources: int = 0
    loser_sources: int = 0


@dataclass
class MergeResult:
    pairs_examined: int = 0
    pairs_merged: int = 0
    merged: list[tuple[str, str]] = field(default_factory=list)  # (winner, loser) rel paths
    errors: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


# ── Discovery ───────────────────────────────────────────────────────


def _read_concept_pages(root: Path) -> list[tuple[Path, str]]:
    """Load all concept pages as (path, content) tuples, excluding protected/index."""
    concepts_dir = root / "semantic" / "concepts"
    if not concepts_dir.exists():
        return []
    out: list[tuple[Path, str]] = []
    for p in sorted(concepts_dir.glob("*.md")):
        if p.name == "index.md" or p.name.endswith(".summary.md"):
            continue
        if p.name in PROTECTED_FILES:
            continue
        try:
            out.append((p, p.read_text(encoding="utf-8")))
        except OSError:
            continue
    return out


def _source_count(content: str) -> int:
    """Read `sources: N` from frontmatter, 0 if absent."""
    m = re.search(r"^sources:\s*(\d+)", content, re.MULTILINE)
    return int(m.group(1)) if m else 0


def _pick_winner(a: Path, a_content: str, b: Path, b_content: str) -> tuple[Path, Path]:
    """Deterministically pick (winner, loser). Prefer more sources, then
    longer content, then older mtime."""
    a_src, b_src = _source_count(a_content), _source_count(b_content)
    if a_src != b_src:
        return (a, b) if a_src > b_src else (b, a)
    if len(a_content) != len(b_content):
        return (a, b) if len(a_content) > len(b_content) else (b, a)
    try:
        return (a, b) if a.stat().st_mtime <= b.stat().st_mtime else (b, a)
    except OSError:
        return (a, b)


def find_merge_pairs(root: Path | None = None) -> list[MergePair]:
    """Scan concepts, return candidate merge pairs (similarity >= threshold).

    Each concept appears in at most one pair per pass -- once flagged as a
    loser, it's removed from subsequent candidacy.
    """
    if root is None:
        root = tree_root()

    pages = _read_concept_pages(root)
    if len(pages) < 2:
        return []

    # Embed each page (first 2000 chars, same policy as clustering)
    texts = [c[:2000] for _, c in pages]
    t0 = time.monotonic()
    embeddings = asyncio.run(_embed_texts(texts))
    log.info(
        "Embedded %d concept pages for merge scan in %dms",
        len(texts), int((time.monotonic() - t0) * 1000),
    )

    arr = np.array(embeddings, dtype=np.float32)
    norms = np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-10)
    normalized = arr / norms
    sim = normalized @ normalized.T

    n = len(pages)
    claimed: set[int] = set()
    pairs: list[MergePair] = []

    # Collect all above-threshold pairs, sort by descending similarity.
    candidates = []
    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] >= _MERGE_THRESHOLD:
                candidates.append((sim[i, j], i, j))
    candidates.sort(reverse=True)

    for s, i, j in candidates:
        if i in claimed or j in claimed:
            continue
        a_path, a_content = pages[i]
        b_path, b_content = pages[j]
        winner, loser = _pick_winner(a_path, a_content, b_path, b_content)
        pairs.append(
            MergePair(
                winner=winner,
                loser=loser,
                similarity=float(s),
                winner_sources=_source_count(a_content if winner == a_path else b_content),
                loser_sources=_source_count(b_content if loser == b_path else a_content),
            )
        )
        claimed.add(i)
        claimed.add(j)

    return pairs


# ── Merge execution ────────────────────────────────────────────────


_MERGE_PROMPT_DIRECTION = (
    "Merge these two concept pages into a single coherent page. "
    "Both cover the same underlying concept from different angles. "
    "Keep the heading and structure of the target page, but integrate any "
    "distinct claims, examples, or wikilinks from the loser page. "
    "Preserve specifics: names, numbers, quotes. Remove redundancy. "
    "The output replaces the target page entirely."
)


def _rewrite_wikilinks_to_loser(
    loser: Path,
    winner: Path,
    root: Path,
) -> int:
    """Rewrite [[loser-stem]] / [[loser/rel/path]] wikilinks to point at
    the winner. Returns number of files touched."""
    loser_rel = loser.relative_to(root).with_suffix("")
    loser_rel_fwd = str(loser_rel).replace("\\", "/")
    loser_stem = loser.stem
    winner_rel = str(winner.relative_to(root).with_suffix("")).replace("\\", "/")

    patterns = [
        (re.compile(rf"\[\[{re.escape(loser_rel_fwd)}\]\]"), f"[[{winner_rel}]]"),
        (re.compile(rf"\[\[{re.escape(loser_rel_fwd)}(\|[^\]]+)\]\]"), rf"[[{winner_rel}\1]]"),
        (re.compile(rf"\[\[{re.escape(loser_stem)}\]\]"), f"[[{winner_rel}]]"),
    ]

    touched = 0
    for md_file in root.rglob("*.md"):
        if ".smriti" in md_file.parts or ".git" in md_file.parts:
            continue
        if md_file == loser:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        new_content = content
        for pat, repl in patterns:
            new_content = pat.sub(repl, new_content)
        if new_content != content:
            md_file.write_text(new_content, encoding="utf-8")
            touched += 1
    return touched


def merge_pair(
    pair: MergePair,
    root: Path,
    *,
    executor_fn: Callable[..., str] = executor_via_claude,
    dry_run: bool = False,
) -> dict:
    """Merge one pair destructively: executor combines, loser deleted,
    wikilinks rewritten, cascade queued."""
    info = {
        "winner": str(pair.winner.relative_to(root)),
        "loser": str(pair.loser.relative_to(root)),
        "similarity": pair.similarity,
        "action": "dry-run" if dry_run else "merged",
        "wikilinks_rewritten": 0,
        "error": "",
    }
    if dry_run:
        log.info(
            "DRY RUN: would merge %s <- %s (sim=%.2f)",
            info["winner"], info["loser"], pair.similarity,
        )
        return info

    try:
        winner_text = pair.winner.read_text(encoding="utf-8")
        loser_text = pair.loser.read_text(encoding="utf-8")
    except OSError as exc:
        info["error"] = f"read failed: {exc}"
        info["action"] = "skipped"
        return info

    # Combine via executor
    merged_content = executor_fn(
        winner_text[:_MAX_MERGE_CONTENT],
        _MERGE_PROMPT_DIRECTION,
        loser_text[:_MAX_MERGE_CONTENT],
    )

    pair.winner.write_text(merged_content, encoding="utf-8")

    # Rewrite wikilinks pointing at loser
    touched = _rewrite_wikilinks_to_loser(pair.loser, pair.winner, root)
    info["wikilinks_rewritten"] = touched

    # Delete the loser file
    try:
        pair.loser.unlink()
    except OSError as exc:
        info["error"] = f"loser unlink failed: {exc}"

    # Cascade from winner
    queue_cognitive_cascade([pair.winner], root)

    log.info(
        "MERGED %s <- %s (sim=%.2f, %d wikilinks rewritten)",
        info["winner"], info["loser"], pair.similarity, touched,
    )
    return info


def merge_all(
    root: Path | None = None,
    *,
    executor_fn: Callable[..., str] = executor_via_claude,
    dry_run: bool = False,
    limit: int | None = None,
) -> MergeResult:
    """Find and merge all above-threshold concept pairs.

    Parameters
    ----------
    root:
        Tree root.
    executor_fn:
        Executor to call for the merge synthesis.
    dry_run:
        If True, only report what would be merged. No writes.
    limit:
        If set, cap to first N pairs (sorted by similarity, highest first).
    """
    if root is None:
        root = tree_root()

    t0 = time.monotonic()
    result = MergeResult()

    pairs = find_merge_pairs(root)
    result.pairs_examined = len(pairs)
    if limit is not None:
        pairs = pairs[:limit]

    for pair in pairs:
        try:
            info = merge_pair(pair, root, executor_fn=executor_fn, dry_run=dry_run)
            if info.get("action") == "merged":
                result.pairs_merged += 1
                result.merged.append((info["winner"], info["loser"]))
            if info.get("error"):
                result.errors.append(f"{info['winner']} <- {info['loser']}: {info['error']}")
        except Exception as exc:
            result.errors.append(f"{pair.winner.name} <- {pair.loser.name}: {exc}")
            log.warning("merge_pair crashed: %s", exc)

    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return result

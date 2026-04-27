"""Batch consolidation — cluster similar files, synthesize concept pages.

Instead of per-file ingest (1 file → 1 summary → 1 route = 2 LLM calls),
batch consolidation clusters pending files by embedding similarity and
produces ONE concept page per cluster (1 LLM call). Reduces O(N) to
O(clusters).

Flow:
1. Embed all pending files locally (ONNX, no LLM cost)
2. Cluster by cosine similarity (numpy, greedy threshold)
3. For each cluster: search for existing concept page → REVISE or CREATE
4. Queue cognitive cascade from changed pages
5. Reindex so leaf files are searchable
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

from smriti.store.judge import CallMetadata, _get_claude_path, executor_via_claude

log = logging.getLogger(__name__)

_MAX_CLUSTER_CONTENT = 40_000  # max chars to send to executor for synthesis
_DEFAULT_THRESHOLD = float(os.environ.get("NARADA_CLUSTER_THRESHOLD", "0.7"))
# Adaptive mode: when fragmentation is high, step down threshold.
_ADAPTIVE_MIN_THRESHOLD = float(os.environ.get("NARADA_CLUSTER_ADAPTIVE_MIN", "0.5"))
_ADAPTIVE_STEP = 0.05
_ADAPTIVE_SINGLETON_BUDGET = 0.4   # stop when singletons <= 40% of files
_ADAPTIVE_MAX_COLLAPSE = 0.8       # reject threshold if largest cluster > 80%

# Concept match thresholds. Kept strict enough that a search miss creates a
# fresh concept page only when there genuinely isn't a close existing one.
# Loose matches (previous default 0.5) produced sprawl: every cluster found
# a "related but not really the same" page and wrote under its own slug.
_CONCEPT_SEARCH_MATCH_MIN = float(
    os.environ.get("NARADA_CONCEPT_SEARCH_MIN", "0.7")
)
# When deciding whether a new concept slug already exists under semantic/concepts/
# (title embedding similarity), this is the cutoff that flips CREATE -> REVISE.
_CONCEPT_TITLE_MATCH_MIN = float(
    os.environ.get("NARADA_CONCEPT_TITLE_MATCH_MIN", "0.85")
)


@dataclass
class ClusterResult:
    """Result of processing one cluster."""

    files: list[Path] = field(default_factory=list)
    concept_page: Path | None = None
    action: str = ""  # "created" | "revised" | "skipped"
    cluster_size: int = 0
    error: str = ""


# ── Embedding + clustering ─────────────────────────────────────────


def _get_embedding_provider():
    """Get the local embedding provider (same one the indexer uses)."""
    from smriti._vendored.memsearch.embeddings import get_provider

    try:
        return get_provider("onnx")
    except Exception:
        try:
            return get_provider("local")
        except Exception:
            raise RuntimeError(
                "No embedding provider available. Install onnxruntime or sentence-transformers."
            )


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts using the local provider."""
    provider = _get_embedding_provider()
    return await provider.embed(texts)


def _cosine_similarity_matrix(embeddings: list[list[float]]) -> np.ndarray:
    """Compute pairwise cosine similarity for N embeddings."""
    arr = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)  # avoid division by zero
    normalized = arr / norms
    return normalized @ normalized.T


def _greedy_cluster(
    sim_matrix: np.ndarray,
    threshold: float,
) -> list[list[int]]:
    """Greedy clustering: assign each item to the first cluster it's
    similar enough to, or start a new cluster."""
    n = sim_matrix.shape[0]
    clusters: list[list[int]] = []
    assigned = [False] * n

    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if not assigned[j] and sim_matrix[i][j] >= threshold:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)

    return clusters


def _embed_paths(paths: list[Path]) -> tuple[list[list[float]], list[Path]]:
    """Read + embed a list of paths. Returns (embeddings, valid_paths).

    Reads the first 2000 chars of each file -- enough to capture topic for
    the embedding model used (all-MiniLM-L6-v2, 384 dim).
    """
    texts: list[str] = []
    valid_paths: list[Path] = []
    for p in paths:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")[:2000]
            texts.append(content)
            valid_paths.append(p)
        except OSError:
            log.warning("Cannot read %s for clustering, skipping", p)

    if not texts:
        return [], []

    t0 = time.monotonic()
    embeddings = asyncio.run(_embed_texts(texts))
    embed_ms = int((time.monotonic() - t0) * 1000)
    log.info("Embedded %d files in %dms", len(texts), embed_ms)
    return embeddings, valid_paths


def cluster_files(
    paths: list[Path],
    *,
    similarity_threshold: float = _DEFAULT_THRESHOLD,
    adaptive: bool = False,
) -> list[list[Path]]:
    """Embed files locally and cluster by cosine similarity.

    Parameters
    ----------
    paths:
        Files to cluster.
    similarity_threshold:
        Cosine similarity cutoff. When ``adaptive=False`` (default), this
        is used as-is. When ``adaptive=True``, it's the starting (maximum)
        threshold; the function steps down if fragmentation is high.
    adaptive:
        If True, automatically loosen the threshold when >40% of files
        would end up as singletons, bottoming out at NARADA_CLUSTER_ADAPTIVE_MIN
        (default 0.5). Refuses to loosen below a point where one cluster
        exceeds 80% of total files (over-collapse).

    Returns a list of clusters, each cluster being a list of Paths.
    """
    if not paths:
        return []
    if len(paths) == 1:
        return [paths]

    embeddings, valid_paths = _embed_paths(paths)
    if not embeddings:
        return []

    sim_matrix = _cosine_similarity_matrix(embeddings)

    if adaptive and len(valid_paths) >= 5:
        index_clusters, chosen_threshold = _adaptive_cluster(
            sim_matrix, similarity_threshold,
        )
    else:
        index_clusters = _greedy_cluster(sim_matrix, similarity_threshold)
        chosen_threshold = similarity_threshold

    path_clusters = [[valid_paths[i] for i in cluster] for cluster in index_clusters]
    singletons = sum(1 for c in path_clusters if len(c) == 1)
    log.info(
        "Clustered %d files into %d clusters (threshold=%.2f, %d singletons, adaptive=%s)",
        len(valid_paths), len(path_clusters), chosen_threshold, singletons, adaptive,
    )

    return path_clusters


def _adaptive_cluster(
    sim_matrix: np.ndarray,
    start_threshold: float,
) -> tuple[list[list[int]], float]:
    """Try thresholds from start_threshold down to _ADAPTIVE_MIN_THRESHOLD.

    Pick the first threshold where:
      - singleton ratio <= _ADAPTIVE_SINGLETON_BUDGET, AND
      - largest cluster does not exceed _ADAPTIVE_MAX_COLLAPSE of total.

    If no threshold satisfies both, return the best candidate: lowest
    singleton ratio that doesn't over-collapse. Falls back to the start
    threshold if over-collapse hits immediately.
    """
    n = sim_matrix.shape[0]
    best: tuple[list[list[int]], float, float] | None = None  # (clusters, threshold, singleton_ratio)

    thresh = start_threshold
    while thresh >= _ADAPTIVE_MIN_THRESHOLD - 1e-9:
        clusters = _greedy_cluster(sim_matrix, thresh)
        singletons = sum(1 for c in clusters if len(c) == 1)
        sr = singletons / n
        max_frac = max(len(c) for c in clusters) / n if clusters else 0.0

        if max_frac > _ADAPTIVE_MAX_COLLAPSE:
            # Too collapsed. Prior threshold was better.
            log.debug(
                "Adaptive: threshold=%.2f over-collapses (%.0f%% in one cluster), stopping",
                thresh, max_frac * 100,
            )
            break

        # Track best seen (lowest singleton ratio)
        if best is None or sr < best[2]:
            best = (clusters, thresh, sr)

        if sr <= _ADAPTIVE_SINGLETON_BUDGET:
            log.info(
                "Adaptive: chose threshold=%.2f (singletons=%.0f%%, under %.0f%% budget)",
                thresh, sr * 100, _ADAPTIVE_SINGLETON_BUDGET * 100,
            )
            return clusters, thresh

        thresh -= _ADAPTIVE_STEP

    if best is None:
        # Only reachable if over-collapse hit immediately at start_threshold.
        return _greedy_cluster(sim_matrix, start_threshold), start_threshold

    log.info(
        "Adaptive: fell through to threshold=%.2f (best singleton ratio %.0f%%)",
        best[1], best[2] * 100,
    )
    return best[0], best[1]


# ── Cluster → concept page ─────────────────────────────────────────


def _build_cluster_content(cluster: list[Path]) -> str:
    """Concatenate cluster files for the executor, with headers."""
    parts: list[str] = []
    total = 0
    for p in cluster:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"## Source: {p.name}\n\n"
        available = _MAX_CLUSTER_CONTENT - total
        if available <= 0:
            break
        text = header + content[:available]
        parts.append(text)
        total += len(text)

    return "\n\n---\n\n".join(parts)


def _search_for_existing_concept(
    cluster_content: str,
    root: Path,
) -> tuple[Path | None, str]:
    """Search the tree for an existing concept page matching this cluster.

    Returns (path, existing_content) or (None, "") if no match.
    Only considers non-leaf pages (concepts, projects, goals).
    """
    from smriti.core.tree import smriti_db_path
    from smriti.store.router import is_leaf_path
    from smriti.store.schema import ensure_schema
    from smriti.store.search import search

    db_path = smriti_db_path()
    if not db_path.exists():
        return None, ""

    import sqlite3
    conn_tmp = sqlite3.connect(str(db_path))
    row = conn_tmp.execute("SELECT value FROM meta WHERE key = 'dimension'").fetchone()
    dim = int(row[0]) if row else 384
    conn_tmp.close()

    conn = ensure_schema(db_path, dim)
    try:
        # Search using the first 500 chars as query (topic-level)
        results = search(conn, cluster_content[:500], top_k=5, use_reranker=False)
    finally:
        conn.close()

    for r in results:
        if is_leaf_path(r.source):
            continue
        if r.score < _CONCEPT_SEARCH_MATCH_MIN:
            continue
        full_path = root / r.source
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                return full_path, content
            except OSError:
                continue

    return None, ""


def _find_duplicate_concept_by_title(
    slug: str,
    root: Path,
) -> Path | None:
    """Last-line guard against sprawl: check if a semantic/concepts/*.md file
    already exists whose title embedding is >= _CONCEPT_TITLE_MATCH_MIN
    similar to the new slug. Returns the matching path or None.

    Prevents the pathology where a search miss routes to CREATE but a
    near-synonym concept already lives in the wiki (e.g. sovereignty.md
    + sovereign-trust.md both existing as separate pages).
    """
    concepts_dir = root / "semantic" / "concepts"
    if not concepts_dir.exists():
        return None

    candidates = [
        p for p in concepts_dir.glob("*.md")
        if p.name != "index.md" and not p.name.endswith(".summary.md")
    ]
    if not candidates:
        return None

    # Build texts for embedding: new slug vs existing filenames (stems)
    texts = [slug.replace("-", " ").replace("_", " ")]
    for p in candidates:
        texts.append(p.stem.replace("-", " ").replace("_", " "))

    try:
        embeddings = asyncio.run(_embed_texts(texts))
    except Exception as exc:
        log.warning("Could not embed titles for duplicate check: %s", exc)
        return None

    arr = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)
    normalized = arr / norms
    new_vec = normalized[0]
    sims = normalized[1:] @ new_vec

    best_idx = int(np.argmax(sims))
    best_sim = float(sims[best_idx])
    if best_sim >= _CONCEPT_TITLE_MATCH_MIN:
        log.info(
            "Duplicate-title guard: new slug '%s' ~ existing '%s' (sim=%.2f). "
            "Routing to REVISE instead of CREATE.",
            slug, candidates[best_idx].stem, best_sim,
        )
        return candidates[best_idx]
    return None


def consolidate_cluster(
    cluster: list[Path],
    root: Path,
    *,
    executor_fn: Callable[..., str] = executor_via_claude,
) -> ClusterResult:
    """Process one cluster: search for existing concept → REVISE or CREATE."""
    from smriti.store.cascade import PROTECTED_FILES, queue_cognitive_cascade

    result = ClusterResult(files=cluster, cluster_size=len(cluster))
    cluster_content = _build_cluster_content(cluster)

    if not cluster_content.strip():
        result.action = "skipped"
        result.error = "empty cluster content"
        return result

    # Search for existing concept page
    existing_path, existing_content = _search_for_existing_concept(cluster_content, root)

    if existing_path and existing_path.name not in PROTECTED_FILES:
        # REVISE existing concept page
        direction = (
            f"Update this concept page with new information from {len(cluster)} "
            f"related source files. Preserve existing content, integrate new "
            f"findings. Add wikilinks to related concepts where appropriate."
        )
        try:
            revised = executor_fn(existing_content, direction, cluster_content)
            existing_path.write_text(revised, encoding="utf-8")
            result.concept_page = existing_path
            result.action = "revised"
            log.info(
                "Revised existing concept: %s (from %d files)",
                existing_path.relative_to(root), len(cluster),
            )
            queue_cognitive_cascade([existing_path], root)
            from smriti.store.source_registry import mark_consolidated
            mark_consolidated(cluster, root=root)
        except Exception as e:
            result.action = "skipped"
            result.error = str(e)
            log.warning("Failed to revise %s: %s", existing_path, e)
    else:
        # CREATE new concept page (unless a near-duplicate already exists)
        direction = (
            f"Synthesize a concept page from these {len(cluster)} related source "
            f"files. Create a clear, structured markdown page with a heading that "
            f"captures the topic. Preserve key details and conclusions. Include "
            f"wikilinks to related concepts using [[concept-name]] syntax."
        )
        try:
            content = executor_fn(cluster_content, direction, cluster_content)
            slug = _topic_slug(cluster[0], content)

            # Duplicate-title guard: if a concept with near-identical slug
            # already exists, revise it instead of creating sprawl.
            duplicate = _find_duplicate_concept_by_title(slug, root)
            if duplicate is not None:
                try:
                    existing_text = duplicate.read_text(encoding="utf-8")
                except OSError:
                    existing_text = ""
                revise_direction = (
                    f"Merge new information from {len(cluster)} source files into "
                    f"this existing concept page. Preserve existing content, "
                    f"integrate new findings, add wikilinks to related concepts."
                )
                revised = executor_fn(existing_text, revise_direction, cluster_content)
                duplicate.write_text(revised, encoding="utf-8")
                result.concept_page = duplicate
                result.action = "revised"
                log.info(
                    "Revised (via duplicate-title guard) %s from %d files",
                    duplicate.relative_to(root), len(cluster),
                )
                queue_cognitive_cascade([duplicate], root)
                from smriti.store.source_registry import mark_consolidated
                mark_consolidated(cluster, root=root)
                return result

            concept_path = root / "semantic" / "concepts" / f"{slug}.md"
            concept_path.parent.mkdir(parents=True, exist_ok=True)
            # Add frontmatter
            page = f"---\ncreated_by: consolidate\nsources: {len(cluster)}\n---\n\n{content}\n"
            concept_path.write_text(page, encoding="utf-8")
            result.concept_page = concept_path
            result.action = "created"
            log.info(
                "Created concept: %s (from %d files)",
                concept_path.relative_to(root), len(cluster),
            )
            queue_cognitive_cascade([concept_path], root)
            from smriti.store.source_registry import mark_consolidated
            mark_consolidated(cluster, root=root)
        except Exception as e:
            result.action = "skipped"
            result.error = str(e)
            log.warning("Failed to create concept for cluster: %s", e)

    return result


# ── Batch orchestrator ──────────────────────────────────────────────


def batch_consolidate(
    paths: list[Path],
    root: Path,
    *,
    similarity_threshold: float = _DEFAULT_THRESHOLD,
    executor_fn: Callable[..., str] = executor_via_claude,
    reindex: bool = True,
    on_cluster_done: Callable[["ClusterResult"], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> list[ClusterResult]:
    """Full batch pipeline: cluster files -> process each cluster.

    Parameters
    ----------
    paths, root, similarity_threshold, executor_fn, reindex:
        Standard inputs.
    on_cluster_done:
        Optional callback invoked after each cluster is processed (success
        OR failure). Use this to commit per-cluster work durably -- e.g.
        the sleep dispatcher marks the cluster's source files as complete
        in the queue here, so killing the process loses at most one
        in-flight cluster instead of all of them.
    should_continue:
        Optional callable returning True if the cluster loop should keep
        going. Called BEFORE each cluster (not before the first). Lets
        callers enforce a soft time budget -- when False, the loop exits
        cleanly and the caller can still run trailing wrap-up (Stages 3/4,
        audit, etc.) instead of getting kill-9'd by the shell.
    """
    t0 = time.monotonic()

    # Filter to existing files
    valid = [p for p in paths if p.exists()]
    if not valid:
        return []

    log.info("Batch consolidate: %d files", len(valid))

    # Cluster (adaptive: loosen threshold when fragmentation is high)
    clusters = cluster_files(
        valid, similarity_threshold=similarity_threshold, adaptive=True,
    )
    log.info(
        "Clustering: %d files -> %d clusters",
        len(valid), len(clusters),
    )

    # Process each cluster
    results: list[ClusterResult] = []
    stopped_early = False
    for i, cluster in enumerate(clusters):
        # Soft time-budget check (between clusters, not mid-cluster). The
        # first cluster always runs even if budget is already tight.
        if i > 0 and should_continue is not None and not should_continue():
            log.info(
                "Budget exhausted; stopping after %d/%d clusters (%d remaining)",
                i, len(clusters), len(clusters) - i,
            )
            stopped_early = True
            break

        log.info(
            "Processing cluster %d/%d (%d files)",
            i + 1, len(clusters), len(cluster),
        )
        try:
            r = consolidate_cluster(cluster, root, executor_fn=executor_fn)
            results.append(r)
        except Exception as e:
            log.warning("Cluster %d failed: %s", i + 1, e)
            r = ClusterResult(
                files=cluster,
                cluster_size=len(cluster),
                action="skipped",
                error=str(e),
            )
            results.append(r)

        # Per-cluster commit hook: caller can mark queue tasks done here
        # so killing the process loses at most one in-flight cluster.
        if on_cluster_done is not None:
            try:
                on_cluster_done(r)
            except Exception as cb_exc:
                log.warning("on_cluster_done callback failed: %s", cb_exc)

    if stopped_early:
        # Tag remaining unprocessed clusters as skipped so the caller can
        # report them. We don't call on_cluster_done for these -- their
        # source files should remain "pending" in the queue for the next
        # sleep to pick up.
        pass

    # Reindex so leaf files are searchable
    if reindex:
        from smriti.store.indexer import index_tree
        index_tree(root=root)

    elapsed = int((time.monotonic() - t0) * 1000)
    created = sum(1 for r in results if r.action == "created")
    revised = sum(1 for r in results if r.action == "revised")
    skipped = sum(1 for r in results if r.action == "skipped")
    log.info(
        "Batch consolidate complete: %d clusters, %d created, %d revised, "
        "%d skipped, %dms",
        len(results), created, revised, skipped, elapsed,
    )

    from smriti.metrics import get_logger
    get_logger().log(
        "batch_consolidate",
        files_total=len(valid),
        clusters=len(results),
        created=created,
        revised=revised,
        skipped=skipped,
        elapsed_ms=elapsed,
    )

    return results


# ── Utilities ──────────────────────────────────────────────────────


def _topic_slug(first_file: Path, content: str) -> str:
    """Derive a slug for a new concept page."""
    # Try to extract the first heading
    match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    if match:
        title = match.group(1).strip()
    else:
        title = first_file.stem

    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:60] or "untitled-concept"

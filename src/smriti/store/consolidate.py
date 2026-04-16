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


def cluster_files(
    paths: list[Path],
    *,
    similarity_threshold: float = _DEFAULT_THRESHOLD,
) -> list[list[Path]]:
    """Embed files locally and cluster by cosine similarity.

    Returns a list of clusters, each cluster being a list of Paths.
    """
    if not paths:
        return []
    if len(paths) == 1:
        return [paths]

    # Read file contents (first 2000 chars for embedding — enough for topic)
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
        return []

    # Embed
    t0 = time.monotonic()
    embeddings = asyncio.run(_embed_texts(texts))
    embed_ms = int((time.monotonic() - t0) * 1000)
    log.info("Embedded %d files in %dms", len(texts), embed_ms)

    # Cluster
    sim_matrix = _cosine_similarity_matrix(embeddings)
    index_clusters = _greedy_cluster(sim_matrix, similarity_threshold)

    # Map indices back to paths
    path_clusters = [[valid_paths[i] for i in cluster] for cluster in index_clusters]
    log.info(
        "Clustered %d files into %d clusters (threshold=%.2f)",
        len(valid_paths), len(path_clusters), similarity_threshold,
    )

    return path_clusters


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
        if r.score < 0.5:
            continue
        full_path = root / r.source
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                return full_path, content
            except OSError:
                continue

    return None, ""


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
        except Exception as e:
            result.action = "skipped"
            result.error = str(e)
            log.warning("Failed to revise %s: %s", existing_path, e)
    else:
        # CREATE new concept page
        direction = (
            f"Synthesize a concept page from these {len(cluster)} related source "
            f"files. Create a clear, structured markdown page with a heading that "
            f"captures the topic. Preserve key details and conclusions. Include "
            f"wikilinks to related concepts using [[concept-name]] syntax."
        )
        try:
            content = executor_fn(cluster_content, direction, cluster_content)
            # Derive path from first file's topic
            slug = _topic_slug(cluster[0], content)
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
) -> list[ClusterResult]:
    """Full batch pipeline: cluster files → process each cluster.

    This is the entry point for the sleep/daemon handler.
    """
    t0 = time.monotonic()

    # Filter to existing files
    valid = [p for p in paths if p.exists()]
    if not valid:
        return []

    log.info("Batch consolidate: %d files", len(valid))

    # Cluster
    clusters = cluster_files(valid, similarity_threshold=similarity_threshold)
    log.info(
        "Clustering: %d files → %d clusters",
        len(valid), len(clusters),
    )

    # Process each cluster
    results: list[ClusterResult] = []
    for i, cluster in enumerate(clusters):
        log.info(
            "Processing cluster %d/%d (%d files)",
            i + 1, len(clusters), len(cluster),
        )
        try:
            r = consolidate_cluster(cluster, root, executor_fn=executor_fn)
            results.append(r)
        except Exception as e:
            log.warning("Cluster %d failed: %s", i + 1, e)
            results.append(ClusterResult(
                files=cluster,
                cluster_size=len(cluster),
                action="skipped",
                error=str(e),
            ))

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

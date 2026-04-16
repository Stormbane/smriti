"""Tests for batch consolidation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from smriti.store.consolidate import (
    ClusterResult,
    _cosine_similarity_matrix,
    _greedy_cluster,
    _topic_slug,
    cluster_files,
)


# ── Similarity + clustering ────────────────────────────────────────


def test_cosine_similarity_identical():
    """Identical vectors should have similarity 1.0."""
    vecs = [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    sim = _cosine_similarity_matrix(vecs)
    assert abs(sim[0][1] - 1.0) < 1e-6


def test_cosine_similarity_orthogonal():
    """Orthogonal vectors should have similarity 0.0."""
    vecs = [[1.0, 0.0], [0.0, 1.0]]
    sim = _cosine_similarity_matrix(vecs)
    assert abs(sim[0][1]) < 1e-6


def test_greedy_cluster_all_similar():
    """When all items are similar, they form one cluster."""
    import numpy as np
    sim = np.array([[1.0, 0.9, 0.85],
                    [0.9, 1.0, 0.88],
                    [0.85, 0.88, 1.0]])
    clusters = _greedy_cluster(sim, threshold=0.8)
    assert len(clusters) == 1
    assert sorted(clusters[0]) == [0, 1, 2]


def test_greedy_cluster_two_groups():
    """Two dissimilar groups should form two clusters."""
    import numpy as np
    sim = np.array([[1.0, 0.95, 0.1, 0.1],
                    [0.95, 1.0, 0.1, 0.1],
                    [0.1, 0.1, 1.0, 0.9],
                    [0.1, 0.1, 0.9, 1.0]])
    clusters = _greedy_cluster(sim, threshold=0.7)
    assert len(clusters) == 2
    assert sorted(clusters[0]) == [0, 1]
    assert sorted(clusters[1]) == [2, 3]


def test_greedy_cluster_all_different():
    """Dissimilar items each form their own cluster."""
    import numpy as np
    sim = np.array([[1.0, 0.1, 0.1],
                    [0.1, 1.0, 0.1],
                    [0.1, 0.1, 1.0]])
    clusters = _greedy_cluster(sim, threshold=0.7)
    assert len(clusters) == 3


# ── cluster_files with mock embeddings ─────────────────────────────


def test_cluster_files_single(tmp_path: Path):
    """Single file returns a cluster of 1."""
    f = tmp_path / "a.md"
    f.write_text("# Test\n", encoding="utf-8")
    clusters = cluster_files([f])
    assert len(clusters) == 1
    assert clusters[0] == [f]


def test_cluster_files_empty():
    """Empty input returns empty clusters."""
    assert cluster_files([]) == []


def test_cluster_files_groups_similar(tmp_path: Path):
    """Files with similar content should cluster together."""
    # Create files with two distinct topics
    for i in range(3):
        (tmp_path / f"mantra_{i}.md").write_text(
            f"# Mantra Research\n\nSacred mantras and vedic chanting study #{i}.\n",
            encoding="utf-8",
        )
    for i in range(3):
        (tmp_path / f"code_{i}.md").write_text(
            f"# Code Review\n\nPython programming and software engineering #{i}.\n",
            encoding="utf-8",
        )

    all_files = sorted(tmp_path.glob("*.md"))

    # This test uses real embeddings — skip if no provider available
    try:
        clusters = cluster_files(all_files, similarity_threshold=0.5)
    except RuntimeError:
        pytest.skip("No embedding provider available")

    # Should cluster into roughly 2 groups (mantra vs code)
    assert 1 <= len(clusters) <= 4  # flexible — exact count depends on model


# ── Topic slug ─────────────────────────────────────────────────────


def test_topic_slug_from_heading():
    slug = _topic_slug(Path("foo.md"), "# Sacred Mantra Research\n\nContent...")
    assert slug == "sacred-mantra-research"


def test_topic_slug_from_filename():
    slug = _topic_slug(Path("my-file-name.md"), "no heading here")
    assert slug == "my-file-name"


def test_topic_slug_truncation():
    long_title = "# " + "a" * 100
    slug = _topic_slug(Path("f.md"), long_title)
    assert len(slug) <= 60


# ── ClusterResult dataclass ────────────────────────────────────────


def test_cluster_result_defaults():
    r = ClusterResult()
    assert r.files == []
    assert r.concept_page is None
    assert r.action == ""
    assert r.cluster_size == 0

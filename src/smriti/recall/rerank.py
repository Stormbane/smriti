"""Trunk-distance reranker on top of the search's candidates.

The keyword search recall runs is structurally blind: it ranks by
relevance regardless of where in the tree a file lives. Smriti's tree
encodes meaning: ``mind/`` and ``open-threads/`` files are load-bearing
identity, ``journal/2026/04/week5/`` files are ephemeral. The recall
hook should prefer the former when scores are close.

Blend formula::

    final = (1 - alpha) * score + alpha * trunk_boost
    trunk_boost = 1.0 / (1.0 + trunk_distance(path))
    # plus: directory manifests (`mind/mind.md`, `open-threads/open-threads.md`)
    # collapse to trunk_boost = 1.0 regardless of nominal depth.

``alpha`` defaults to 0.2 — trunk only breaks ties between similar
scores, doesn't dethrone strong matches.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from smriti.core.tree import tree_root, trunk_distance
from smriti.recall.types import RecallMatch


def _normalize_source(source: str) -> Path | None:
    """A tree-relative source, resolved under the tree root."""
    if not source:
        return None
    return tree_root() / source


def _is_manifest(path: Path, root: Path) -> bool:
    """True for `<dir>/<dir>.md` — the directory's own root file."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) < 2 or not parts[-1].endswith(".md"):
        return False
    return Path(parts[-1]).stem == parts[-2]


def trunk_boost(path: Path, root: Path | None = None) -> float:
    """Score in [0, 1]. Higher = more trunk-adjacent."""
    root = root or tree_root()
    dist = trunk_distance(path, root)
    if dist < 0:
        return 0.0
    base = 1.0 / (1.0 + dist)
    if _is_manifest(path, root):
        base = 1.0
    return base


def rerank(
    matches: list[RecallMatch],
    *,
    alpha: float,
) -> list[RecallMatch]:
    """Blend the relevance score with trunk-distance boost.

    Returns a new list sorted by blended score, highest first. Original
    matches are not mutated. ``alpha == 0`` is a fast no-op.
    """
    if alpha <= 0 or not matches:
        return list(matches)

    root = tree_root()
    rescored: list[RecallMatch] = []
    for m in matches:
        path = _normalize_source(m.source)
        boost = trunk_boost(path, root) if path else 0.0
        new_score = (1.0 - alpha) * m.score + alpha * boost
        rescored.append(replace(m, score=new_score))
    rescored.sort(key=lambda m: m.score, reverse=True)
    return rescored

"""The Hugging Face Hub commits smriti's default models load at.

smriti's own module, not upstream's. A model fetched from the Hub by its
name alone is whatever the repository's ``main`` holds at the time: a change
upstream, or a compromised repository, would change what runs and what the
index's vectors are. The models smriti chooses itself (each embedding
provider's default and the reranker's) load at the commit named here, from
the cache and from the Hub alike, and a commit names its files' content. A
model named any other way has no pin and loads at ``main``.

The Hub's cache names each snapshot by its commit, so the revision an index
records for a pinned model (``smriti.embed.identity.model_revision``) is the
commit here. Moving a pin moves that identity: an index made at the old
commit then needs ``smriti index --full``.
"""

from __future__ import annotations

# Keyed by the name smriti loads the model by: embeddings.DEFAULT_MODELS
# and reranker.DEFAULT_RERANKER.
PINNED_REVISIONS: dict[str, str] = {
    "gpahal/bge-m3-onnx-int8": "2b34e84df040034d4b9eabb62383a87c18955822",
    "all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "Alibaba-NLP/gte-reranker-modernbert-base": "f7481e6055501a30fb19d090657df9ec1f79ab2c",
}


def pinned_revision(model: str) -> str | None:
    """The commit *model* loads at, or None (the repository's ``main``) when it has no pin."""
    return PINNED_REVISIONS.get(model)

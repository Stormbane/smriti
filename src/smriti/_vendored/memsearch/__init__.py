"""Vendored utility modules from zilliztech/memsearch.

Copied 2026-04-12 from https://github.com/zilliztech/memsearch
(commit cloned via `git clone --depth 1` to `C:/Projects/memsearch`).
Original license: MIT — see `LICENSE` in this directory, also
preserved at the smriti repository root `NOTICE.md`.

These modules are **utilities** — file chunking, cross-encoder
reranking, path scanning, embedding providers. They are
deliberately boundary-free: smriti's own identity and memory
model are built separately and do not depend on memsearch's
philosophy or data model.

smriti does NOT vendor memsearch's `core.py`, `store.py`,
`compact.py`, `cli.py`, or `config.py` — those encode memsearch's
architectural choices (Milvus coupling, impersonal data model,
generic consolidation prompt) that conflict with smriti's
sovereignty-first design. smriti writes its own versions of
those components.

Modifications from upstream:
- Recorded per file: in the docstring at the top of a modified module,
  or, in embeddings/ (whose modules carry no header of their own), in
  its __init__ and in comments where the code differs. sizes.py and
  embeddings/stub.py are smriti's own.
"""

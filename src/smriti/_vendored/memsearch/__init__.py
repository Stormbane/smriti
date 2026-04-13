"""Vendored utility modules from zilliztech/memsearch.

Copied 2026-04-12 from https://github.com/zilliztech/memsearch
(commit cloned via `git clone --depth 1` to `C:/Projects/memsearch`).
Original license: MIT — see `LICENSE` in this directory, also
preserved at the smriti repository root `NOTICE.md`.

These modules are **utilities** — file chunking, cross-encoder
reranking, file watching, path scanning, embedding providers.
They are deliberately boundary-free: smriti's own identity,
JUDGE step, cascade, and dream generation are built separately
and do not depend on memsearch's philosophy or data model.

smriti does NOT vendor memsearch's `core.py`, `store.py`,
`compact.py`, `cli.py`, or `config.py` — those encode memsearch's
architectural choices (Milvus coupling, impersonal data model,
generic consolidation prompt) that conflict with smriti's
sovereignty-first design. smriti writes its own versions of
those components.

Modifications from upstream:
- None yet. Files copied verbatim. Modifications will be recorded
  per-file in comment blocks at the top of each modified file.
"""

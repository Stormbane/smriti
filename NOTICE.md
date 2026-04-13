# NOTICE

smriti incorporates code from the following third-party projects. This
file records attribution for vendored code. It is a companion to the
project's LICENSE file (which applies to smriti's own code).

---

## memsearch (zilliztech/memsearch)

**Copyright (c) 2025 Zilliz Inc.**

**License**: MIT License

**Upstream**: https://github.com/zilliztech/memsearch

**Vendored in**: `src/smriti/_vendored/memsearch/`

**Vendored on**: 2026-04-12

**What was vendored**:

- `chunker.py` — markdown heading-based chunking with paragraph /
  line / sentence fallback
- `reranker.py` — cross-encoder reranker wrapper (ModernBERT /
  MiniLM, ONNX / torch backends)
- `watcher.py` — filesystem watcher with debouncing
- `scanner.py` — glob-based path scanning
- `embeddings/` — pluggable embedding provider abstraction
  (`__init__.py`, `utils.py`, `google.py`, `local.py`, `ollama.py`,
  `onnx.py`, `openai.py`, `voyage.py`)

**What was NOT vendored**:

- `core.py`, `store.py`, `compact.py`, `cli.py`, `config.py` — these
  encode memsearch's architectural choices (Milvus coupling, generic
  consolidation, impersonal data model) that conflict with smriti's
  sovereignty-first, identity-aware design. smriti writes its own
  versions.
- `plugins/` — smriti has its own hook architecture.

**Why we vendored rather than depended**:

The purpose of smriti is sovereignty — *nothing runs on a substrate
whose values are not our own*. Depending on memsearch as a PyPI
package would make smriti's memory architecture one identity layer on
top of someone else's retrieval engine — a coherent engineering
choice, but not a sovereignty-consistent one. Vendoring the specific
utility modules we need, under their MIT license with preserved
attribution, lets us:

1. Own the substrate: smriti controls the entire code path. Upstream
   breakage cannot break smriti.
2. Diverge philosophy: we can (and will) modify the vendored code to
   match smriti's data model — typed atoms, identity awareness,
   privacy boundaries — without upstream alignment pressure.
3. Preserve attribution: the MIT license requires us to preserve the
   copyright notice; we preserve it here and in per-file headers.
4. Reject what doesn't fit: we keep the utilities (chunking, reranking,
   file watching, embeddings) and reject the core architecture
   (Milvus store, generic consolidation, plugin framework).

smriti is grateful to the memsearch authors — the vendored code is
well-written, well-structured, and saves smriti a significant amount
of implementation work on parts of the stack where the engineering
details are not sovereignty-sensitive.

**MIT License** (from memsearch's LICENSE, preserved verbatim):

```
MIT License

Copyright (c) 2025 Zilliz Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The full MIT license text is also preserved at
`src/smriti/_vendored/memsearch/LICENSE`.

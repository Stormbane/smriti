# Architecture

<!-- USAGE: Technical blueprint for the project.
     Read this before making structural changes or adding new components.
     Linked from CLAUDE.md as a reference doc — read on demand, not pre-loaded.
     docs/ARCHITECTURE.md is the April 2026 design; this is the quick reference
     to the code as built. -->

## Tech Stack

- **Language**: Python 3.11+
- **Type checking**: mypy (strict)
- **Linting**: ruff (line-length 100)
- **Storage**: SQLite (WAL mode) with sqlite-vec for vectors, FTS5 for keyword search
- **Embeddings**: ONNX runtime (offline, local) as primary; sentence-transformers as fallback
- **LLM backend**: pluggable providers in `smriti.llm` (`claude -p` by default, then
  `codex exec`, the Anthropic and OpenAI SDKs, Ollama), used by the nightly digest and
  the morning message
- **MCP**: JSON-RPC over stdio
- **Dependencies**: Zero runtime deps in base install; optional extras for read, api,
  codex, private, witness, dev
- **Build**: hatchling
- **Tests**: pytest

## Project Structure

```
src/smriti/
  cli.py                   # the `smriti` command (index, read, write, status, doctor,
                           #   nightly, witness, morning, logd, tasks, eval, metrics, recall)
  mcp/                     # MCP server: server.py (JSON-RPC), tool.py (Tool, ToolError), tools/ by group
  mcp_server.py            # entry-point shim for installed configs (python -m smriti.mcp_server)
  metrics.py               # structured JSONL event logging with rotation
  doctor.py                # verify a harness bridge without changing state
  witness.py, backup.py    # tamper-evidence; the backup trigger

  core/                    # tree_root(), OS locks, edit_lock/write_atomic, safe names, git
  store/
    schema.py              # SQLite schema (chunks, sources, meta, FTS5, vec0)
    index_policy.py        # what the index covers
    indexer.py             # scan, chunk, embed, upsert (incremental)
    search.py              # hybrid vector+FTS5 search, trunk-distance scoring, reranking
    writer.py              # write dated entries, index the file, update index pages
    index_pages.py         # regenerate parent index.md listings (no LLM)
  daylog/                  # capture adapters, logd, render, digest, rollups, nightly,
                           #   morning, schedule
  wake/                    # the wake briefing and its presence sections
  llm/                     # LLM providers
  recall/                  # ambient-recall hook backends (smriti, qmd)
  integrations/            # claude_code, codex, hermes installers; shared hooks
  eval/                    # search evaluation: cases, runner, synthetic fixture
  private/                 # encrypted private store
  _vendored/memsearch/     # bundled chunker, reranker, embeddings, scanner

scripts/
  install.py               # full install (mirrors, hooks, MCP, CLAUDE.md)
  setup_project.py         # per-project setup (template, gitignore, mirrors)
  doc_facts.py             # the generated counts in README.md, CLAUDE.md, AGENTS.md

narada/.smriti/            # wake.py, backup.py, narada-p.sh, copied into <root>/.smriti/
project_template/          # skeleton for new projects
memory_template/           # skeleton for a new memory root
```

## Data Flow

### The April design's six steps

```
Experience -> CAPTURE -> EXTRACT -> JUDGE -> WRITE -> CROSSLINK -> INDEX
```

This is the design in docs/ARCHITECTURE.md. Only WRITE and INDEX exist as
designed; CAPTURE is the day-log. EXTRACT, JUDGE and CROSSLINK don't exist (a
batch JUDGE and cascade were built and removed in November 2026).

### Write flow (what happens on `smriti write` / `smriti_write`)
1. Compute path: `{branch}/{YYYY}/{MM-DD}.md`, or
   `journal/{YYYY}/{MM}/week{N}/{MM-DD}.md` for the journal
2. Append the entry with frontmatter (date, time, branch, source) under the
   file's edit lock
3. Incremental reindex (that one file)
4. Index pages: walk up the tree, regenerating each existing `index.md`
   from its directory listing under its edit lock

### Search flow (what happens on `smriti read`)
1. Embed query with same provider as index
2. Vector search via sqlite-vec (k=20)
3. FTS5 keyword search (k=20)
4. Merge, score: VEC * 0.5 + FTS * 0.3 + TRUNK * 0.2
5. Optional cross-encoder reranking (the CLI reranks; the MCP path doesn't)
6. Return top-k results with source, heading, content, score

### The nightly (`smriti nightly`, 03:00)
reconcile -> render -> digest -> rollups -> index -> witness -> status. See
`.ai/features/nightly-cycle.md`.

## API Design

### MCP tools (JSON-RPC over stdio)
- `smriti_read(query, top_k=5)` -- hybrid search, returns ranked results
- `smriti_write(content, branch="journal", title?, source?)` -- write entry, returns path
- `smriti_status()` -- index stats
- Project-workflow, handoff and reference-setup tools: see the README

### Key data structures
- `SearchResult` -- source, heading, content, score, trunk_distance, chunk_id

# Architecture

<!-- USAGE: Fill in during the first session. Technical blueprint for the project.
     Read this before making structural changes or adding new components.
     Linked from CLAUDE.md as a reference doc — read on demand, not pre-loaded.
     For the full design doc, see docs/ARCHITECTURE.md. This is the quick reference. -->

## Tech Stack

- **Language**: Python 3.11+
- **Type checking**: mypy (strict)
- **Linting**: ruff (line-length 100)
- **Storage**: SQLite (WAL mode) with sqlite-vec for vectors, FTS5 for keyword search
- **Embeddings**: ONNX runtime (offline, local) as primary; sentence-transformers as fallback
- **LLM backend**: Anthropic SDK with prompt caching (JUDGE: haiku-4.5, EXECUTOR: sonnet-4.6); fallback to claude -p subprocess
- **MCP**: JSON-RPC over stdio
- **Dependencies**: Zero runtime deps in base install; optional extras for read, api, private, dev
- **Build**: hatchling
- **Tests**: pytest

## Project Structure

```
src/smriti/
  __init__.py              # package metadata
  cli.py                   # 11 CLI commands (index, read, write, status, watch, sleep, queue, daemon, eval, ingest, metrics)
  mcp_server.py            # MCP server (smriti_read, smriti_write, smriti_status)
  metrics.py               # structured JSONL event logging with rotation
  watcher.py               # filesystem watcher, queues ingest/route tasks

  core/
    tree.py                # tree_root(), trunk_distance(), smriti_db_path()

  store/                   # the pipeline
    schema.py              # SQLite schema (chunks, FTS5, vec0, meta)
    indexer.py             # scan, chunk, embed, upsert (incremental)
    search.py              # hybrid vector+FTS5 search, trunk-distance scoring, reranking
    writer.py              # write dated entries, structural cascade, reindex
    judge.py               # JUDGE/EXECUTOR functions (stubs + Anthropic API + claude -p)
    cascade.py             # structural cascade (index.md) + cognitive cascade (JUDGE->EXECUTOR loop)
    queue.py               # JSON task queue (ingest, route, cognitive_cascade, reindex)
    router.py              # search-informed routing: REVISE/LINK/TASK/CREATE actions
    consolidate.py         # batch clustering + concept page synthesis
    ingest.py              # read -> summarize -> route -> execute -> cascade
    api_backend.py         # Anthropic SDK with prompt caching; claude -p fallback

  eval/                    # evaluation framework
  hooks/                   # precompact_capture.py
  private/                 # encrypted layer (skeleton)
  _vendored/memsearch/     # bundled chunker, reranker, embeddings, watcher, scanner

scripts/
  install.py               # full install (mirrors, hooks, MCP, CLAUDE.md)
  setup_project.py         # per-project setup (template, gitignore, mirrors)

narada/                    # wake system templates
  wake.md                  # load-list config
  .smriti/wake.py          # SessionStart loader
  .smriti/narada-p.sh      # headless wake wrapper

project_template/          # skeleton for new projects
```

## Data Flow

### Six-step pipeline (no bypass)

```
Experience -> CAPTURE -> EXTRACT -> JUDGE -> WRITE -> CROSSLINK -> INDEX
               |           |          |
            normalize    propose    approve/reject
                       (frontier)  (identity core)
```

1. **CAPTURE** -- normalize input (conversation, heartbeat, document, URL, manual)
2. **EXTRACT** -- capability LLM proposes memory candidates
3. **JUDGE** -- identity core decides KEEP / REVISE / PROMOTE / DISCARD
4. **WRITE** -- approved candidates land as dated markdown with frontmatter
5. **CROSSLINK** -- entity resolution + wikilink propagation (not yet built)
6. **INDEX** -- vector embedding + FTS5 update (debounced, incremental)

### Write flow (what happens on `smriti write`)
1. Compute path: `{branch}/{YYYY}/{MM-DD-NNN}.md`
2. Write file with frontmatter (date, time, branch, source)
3. Incremental reindex (one file)
4. Structural cascade: walk up tree, regen parent index.md files
5. Queue cognitive cascade for upstream review

### Search flow (what happens on `smriti read`)
1. Embed query with same provider as index
2. Vector search via sqlite-vec (k=20)
3. FTS5 keyword search (k=20)
4. Merge, score: VEC * 0.5 + FTS * 0.3 + TRUNK * 0.2
5. Optional cross-encoder reranking
6. Return top-k results with source, heading, content, score

### Cascade flow (cognitive)
1. JUDGE reads parent MOC + recently changed child
2. Verdict: KEEP (stop) / REVISE (EXECUTOR rewrites parent) / PROMOTE (escalate)
3. If change: queue parent's parent for review
4. Follow wikilinks: files referencing changed page get queued too
5. Max depth: 5. Most cascades stop at 1-2.

## API Design

### MCP tools (JSON-RPC over stdio)
- `smriti_read(query, top_k=5)` -- hybrid search, returns ranked results
- `smriti_write(content, branch="journal", title?, source?)` -- write entry, returns path
- `smriti_status()` -- index stats

### Key data structures
- `SearchResult` -- source, heading, content, score, trunk_distance, chunk_id
- `JudgmentResult` -- seeing, verdict, direction, reason, meta
- `RoutingAction` -- action (REVISE/LINK/TASK/CREATE), target, content, score
- `QueueTask` -- type, path, parent, priority, status, id
- `IngestResult` -- source, summary_path, routing, actions_executed, cascade_queued

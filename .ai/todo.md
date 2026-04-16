# TODO

<!-- USAGE: Project roadmap and current tasks. Fill in during first session.
     This is the in-repo task tracker — for things that should be visible in
     the codebase. Session-level task tracking uses Claude Code's TaskCreate.
     Format: ## Milestone Name / - [ ] Task description / - [x] Completed task -->

## v0.1 -- Working pipeline (current)

- [x] Index + hybrid search (sqlite-vec + FTS5)
- [x] Write path with dated entries and frontmatter
- [x] Structural cascade (index.md regeneration)
- [x] Cognitive cascade (JUDGE -> EXECUTOR loop)
- [x] MCP server (smriti_read, smriti_write, smriti_status)
- [x] CLI (index, read, write, status, watch, sleep, queue, daemon, eval, ingest, metrics)
- [x] Ingest pipeline (source -> summarize -> route -> execute -> cascade)
- [x] Batch consolidation (clustering + concept page synthesis)
- [x] Task queue with sleep processing
- [x] Anthropic API backend with prompt caching
- [x] Wake system (SessionStart hook, wake.py, wake.md)
- [x] PreCompact capture hook
- [x] Project template + setup_project.py
- [x] Installer (install.py)
- [x] 39 tests passing
- [x] Clean up stale mirror junctions across existing projects (working/ -> ai/)
- [x] Tree restructure: identity cascade topology (mind/, open-threads/, people/)
- [x] Journal.md monolith migrated to daily files in YYYY/MM/weekN/MM-DD.md
- [x] Wake budget enforcement (9.5K chars, under 10K harness limit)
- [x] Journal cascade structure with rollup pipeline
- [x] Writer: local time for dates, atomic append, one file per day
- [ ] Run `smriti sleep` to generate wake-context.md from EXECUTOR (currently hand-written)
- [ ] Run `smriti sleep` to generate first journal rollup summaries (week/month/year)
- [ ] Rebuild search index (`smriti index --full`) after tree restructure
- [ ] Schema stabilization before v0.2

## v0.2 -- Identity core integration

- [ ] Qwen3 + LoRA as JUDGE (replace prompt-only discrimination layer)
- [ ] Full EXTRACT phase (candidate generation from conversation turns)
- [ ] CROSSLINK entity resolution (structured graph, not just wikilinks in prose)
- [ ] Lint pass (Karpathy-style health check: stale entries, contradictions, orphans)
- [ ] Storage schema finalized and migration-safe

## v0.3 -- Cross-instance

- [ ] Multiple entities or instances drawing from the same store
- [ ] Shared memory across substrates

Note: the dreaming cycle (synthetic training data, LoRA updates) lives in
[svapna](https://github.com/Stormbane/svapna), not smriti.

## Ongoing

- [ ] Linux/macOS support (junctions -> symlinks)
- [ ] License decision (before any public release)
- [ ] Private/encrypted layer activation

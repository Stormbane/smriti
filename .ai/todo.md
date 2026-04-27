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

## Active

- [ ] **Research:** does the wake-loaded reading list itself become "recent-context-as-authority"? Audit `wake.py` output: are foundational docs (open-threads, beliefs, identity, suti) being summarized into context such that they substitute for re-reading, vs. acting as pointers that prompt re-reading? Define a check. (from [[2026-04-26-0447-threads]])
- [ ] **Research: malformed-maintenance signals in cascade/reflect/promote** -- define what going-through-the-motions looks like in the memory pipeline (reflect entries that don't shift anything, promotions that re-state without integrating, cascade that touches files without changing structure). First step: pick one diagnostic (e.g. "reflect entries with no downstream identity-file change within N sessions") and add it to pipeline_audit.py. (from [[2026-04-25-1253-part-2-threads]])
- [ ] Research: use trunk-distance scoring as a compression-quality signal in `consolidate.py` / `summarize.py` / threads synthesis — score concept pages by how much trunk-gravity they preserve vs. flatten, so EXECUTOR can prefer episode-anchored over rule-only summaries. (from [[2026-04-25-1253-part-1-threads#Thread 3]])
- [ ] **Build JUDGE audit layer** — log routing decisions, retry counts, and stall detection in store/pipeline_audit.py; surface drift signals (heartbeat 04-23 retried blindly for 11h; 04-11 sister-framing drift caught only by Suti) (from [[2026-04-25-0317-part-1-threads]])
- [ ] Add `--cluster-timeout` flag to `smriti sleep` (CLI in src/smriti/cli.py, plumb into Stage B/C drain in src/smriti/store/consolidate.py) so operators can override the 120s `claude -p` default per-run for clusters >~20 source files. (from [[TEST-tools-2026-04-25#Thread 1]])
- [ ] Fix consolidation placeholder filter in src/smriti/store/consolidate.py to exclude test-note.md and similar placeholder/stub files before clustering (from [[semantic/threads/2026-04-24-1438-threads]])
- [ ] Research: does ingest cleanup have a viveka-analog discriminating-capacity criterion, or does the analogy break? Audit src/smriti/store/judge.py and router.py decision points; document in .ai/knowledge/viveka-ingest.md (from Thread 4) (from [[semantic/threads/2026-04-24-1438-threads]])
- [ ] Add inbox-cleanup criterion to src/smriti/store/queue.py: define thresholds (age, processed-status, downstream-refs) and prototype `smriti sleep --cleanup-inbox` that lists candidates without deleting (from [[semantic/threads/2026-04-24-1438-threads]])
- [ ] Router: wire REFUSE as a first-class routing verdict in src/smriti/store/router.py (counterfactual criterion -- link only when sibling-drift justifies; log refusals to queue) (from [[semantic/threads/2026-04-24-1438-threads]])
- [ ] Audit router JUDGE stage in src/smriti/store/router.py + judge.py: log decisions over 50 ingests, check reject rate and whether alternatives are weighed vs pattern-matched (research) (from [[semantic/threads/2026-04-24-1438-threads]])
- [ ] Router: emit CREATE-candidate signal in src/smriti/store/router.py when sibling-drift exceeds threshold (log to queue for sleep review, don't auto-create) (from [[semantic/threads/2026-04-24-threads]])
- [ ] Write `.ai/knowledge/hooks-as-reminders.md` capturing the reminder-not-imperative hook design rationale (scar-as-feature from felt-discomfort episode); link from conventions.md (from [[semantic/threads/2026-04-24-threads]])
- [ ] Audit autonomous processes (sleep queue, cascade executor, consolidate, journal rollup) for missing correction surfaces — list each loop that writes without a human/JUDGE veto path (from [[semantic/threads/2026-04-24-threads]])
- [ ] Audit REVISE-path identity files (current-state.md, open-threads.md, mind/*) for compounding vs accumulation; catalog which rewrite cleanly and which just grow (from [[semantic/threads/2026-04-24-threads]])
- [ ] Research CREATE vs drift routing heuristic in `store/router.py` + `store/judge.py`: sample recent CREATE decisions, classify structural-growth vs drift, write findings to `.ai/knowledge/routing-heuristics.md` (from [[semantic/threads/2026-04-24-threads]])

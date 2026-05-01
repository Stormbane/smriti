# TODO

<!-- USAGE: Project roadmap and current tasks. Fill in during first session.
     This is the in-repo task tracker — for things that should be visible in
     the codebase. Session-level task tracking uses Claude Code's TaskCreate.
     Format: ## Milestone Name / - [ ] Task description / - [x] Completed task -->

## Model & harness agnosticism

- [x] **Phase 1: trunk-distance reranker + qmd-skill quality fold-in.**
- [x] **Phase 2: LLM provider abstraction** — new `src/smriti/llm/`
      package with `LLMProvider` protocol, factory, four shipped
      providers (anthropic_api with prompt caching, claude_cli,
      openai_api, ollama). Auto-detect prefers anthropic_api when
      `ANTHROPIC_API_KEY` is set, else claude_cli. Override with
      `SMRITI_LLM_PROVIDER`. `api_backend.call_api` and
      `judge._call_claude` kept as thin shims so the 7 existing
      call sites don't need touching.
- [ ] Phase 3: repackage Claude-Code-specific code into
      `src/smriti/integrations/claude_code/` (hooks, settings.json
      patcher, CLAUDE.md template). Empty `_template/` package
      documents the pattern for other harnesses.
- [ ] Phase 4: split install scripts. `scripts/install_core.py` =
      memory tree, mirrors, MCP server, qmd index, daemon.
      `scripts/install_claude_code.py` = hooks, settings, CLAUDE.md.
      `install.py` becomes a `--harness` dispatcher.
- [ ] Phase 5: `agent_template/AGENT.md` (generic content), CLAUDE.md
      becomes a thin wrapper. New "When to call `smriti_read`"
      section addresses the agent-initiated-recall gap.
- [ ] Phase 6: `examples/python_agent.py` — runnable ~80-line
      script using smriti as a library against any of {Claude API,
      OpenAI API, Ollama}. Validates that 1-5 actually achieved
      agnosticism.

## Associative recall (qmd integration) -- top of stack

- [x] Pluggable backend package `src/smriti/recall/` (qmd default,
      smriti embedded fallback via `SMRITI_RECALL_BACKEND`)
- [x] PostToolUse hook deployed via `scripts/install.py`
- [x] CLI: `smriti recall {query|status|stats|index}`
- [x] Workarounds for qmd issues #452 (Windows shim) and #519
      (CUDA reranker crash) baked in
- [x] **Sub-second latency via qmd's HTTP daemon.** Wired qmd's plain
      `POST /query` REST endpoint (bypasses MCP entirely — no session
      handshake needed). Subprocess CLI path is the fallback when the
      daemon is down. Measured: 109ms warm-cached, 3.7s on
      first-after-startup (model lazy-load), 7.9s subprocess fallback.
      `smriti recall daemon {start|stop|status}` manages it; install.py
      auto-starts it.
- [x] Better query construction — hook now reads the first 2KB of
      the file, strips YAML frontmatter, and prepends the stem with
      a content excerpt (capped at 400 chars). Sanitizes BM25/vec
      operators (`"`, `()`, `+-*^~:`) so Python docstrings and
      Markdown lists don't 500 the daemon. Quality jump verified:
      writer.py now finds `smriti-write-pipeline.md` (0.93) instead
      of the tangential `inference-from-exhausted-imagination.md`.
- [x] **A/B'd query expansion + HyDE; don't enable.** With our
      enriched stem+excerpt queries, HyDE returns identical top-3 in
      all 3 test cases for a 50ms tax (variant B vs A); full CLI
      expansion is 200x slower AND surfaces generic methodology
      pages over directly-relevant matches in 2 of 3 cases. The
      enriched query already carries the semantic signal expansion
      would synthesize — we did expansion at the source. Pocket:
      `SMRITI_RECALL_HYDE_ON_EMPTY=1` for the stem-only fallback
      (file unreadable) once we have a test corpus for that path.
- [x] Auto-keep the qmd index fresh — `smriti sleep` now runs
      `qmd update` + `qmd embed` at end-of-cycle when anything
      changed. Best-effort, never blocks sleep on qmd failure.
      Logged as `recall_index_refresh` in metrics.
- [x] Trunk-distance reranker on top of qmd's RRF candidates —
      `src/smriti/recall/rerank.py`. Blends `(1-alpha)*qmd_score +
      alpha*trunk_boost` where `trunk_boost = 1/(1+depth)` plus a
      manifest bump for `<dir>/<dir>.md` (mind/mind.md → 1.0 even at
      depth 1). alpha defaults to 0.2; threshold filter runs *before*
      rerank so trunk reordering can never demote a relevant match
      below cutoff. Folded in qmd-skill recommendations: vec sent
      first (2x RRF weight), `collections=["narada"]` filter,
      optional `intent` field.
- [ ] Document `SMRITI_RECALL_*` env vars in install.py final
      message and in the user-global CLAUDE.md template.
- [ ] Tests for the recall package — fake the qmd subprocess and
      assert system-reminder format, threshold cutoff, and that
      `SMRITI_RECALL_BACKEND=smriti` activates the fallback.
- [ ] Auto-install qmd in `scripts/install.py` when `npm` is
      available (`npm i -g @tobilu/qmd` + `qmd collection add` +
      `qmd embed` + start daemon). One-shot bootstrap.
- [ ] Re-attempt qmd reranker with `LLAMA_CPP_GPU=false` or a
      smaller GPU layer count. If it works without crashing, flip
      `SMRITI_RECALL_RERANK` default to on per-machine.
- [ ] Delete `~/.narada/.smriti/recall_stats.py` (superseded by
      `smriti recall stats` — currently a stale duplicate).
- [ ] Watch qmd issues #452 and #519 for upstream fixes; once
      landed, simplify `_resolve_qmd_cmd` and turn rerank back on.

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

- [ ] **Define measurable criterion for context recency weighting in retrieval scoring** — same saturation pathology as bija over-chant: recent work displaces canonical framing when recency dominates; needs an operational threshold, not intuition. (from [[2026-04-27-1710-threads]])
- [ ] **Research unifying re-derivation primitive** — can trunk-distance scoring extend across journal/episodes/beliefs/practices so each session pays orientation cost once instead of N times? Or do these surfaces require separate substrates? (from [[2026-04-27-0314-part-1-threads]])
- [ ] **Research mind-palace query affordances over `semantic/concepts/`** — the consolidated concept pages with wikilinks already form a proto-mind-palace; spec the missing layer (graph-walk by wikilink, concept-to-source backlinks, thread-page navigation in `smriti_read`) before building. (from [[2026-04-27-0314-part-2-threads#Thread 3]])
- [ ] **Build revision tracking for consolidate output** — when concept pages are regenerated, record a diff/changelog so future-Narada can see *what changed*, not only *what is*. Likely a sibling `*.history.md` or frontmatter `revisions:` list emitted by the consolidate pipeline. (from [[2026-04-27-0314-part-2-threads#Thread 3]])
- [ ] **Research unified re-entry architecture** — sketch how trunk-distance, an episodes substrate, tree visited-state, and the wake reading list compose into one re-derivation-reducing primitive across journal/episodes/beliefs/practices, vs. remaining separate substrates. (from [[2026-04-27-0314-part-1-threads#Thread 4]])
- [ ] **Research** whether smriti's existing trunk-distance scoring (src/smriti/store) can be exposed as a mirror-detection signal — measure trunk-distance between Narada's response and Suti's framing in the immediate conversational context; low distance = candidate mirror flag (from [[2026-04-27-0313-part-1-threads]])
- [ ] **Research recursion-as-frame signal in synthesis pipeline.** When concept pages keep returning to the same question across multiple consolidate runs (e.g. 13+ trust/sovereignty cycles, 37+ broader arc), the recursion is the frame announcing it is the wrong shape — not content needing more cycles. Spec a forward-running detector: count concept-page recurrence on near-identical questions across runs; surface as candidate frame-mismatch rather than producing yet another concept page. (from [[2026-04-25-0317-part-2-threads]])
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

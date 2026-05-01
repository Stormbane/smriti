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


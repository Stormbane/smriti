# TODO

<!-- USAGE: Project roadmap and current tasks. Fill in during first session.
     This is the in-repo task tracker — for things that should be visible in
     the codebase. Session-level task tracking uses Claude Code's TaskCreate.
     Format: ## Milestone Name / - [ ] Task description / - [x] Completed task -->

## Next up (chosen 2026-11-04; plan: .ai/features/smriti-renewal.md, Phase 7)

Not started. The renewal plan's Phase 1.0 groundwork (run lock, generation
publish, per-lens scheduling) comes before any new nightly lens.

- [ ] **Intentions (prospective memory).** Promises, follow-ups and repairs
      as typed cue → intention records (`intentions/events.jsonl`), written
      with a `smriti_intend` tool and matched deterministically (time,
      project, person, topic) in a pinned wake section. An intention shown
      twice without being closed is marked lapsed and counted, never dropped.
      Evidence: a typed intention store far outperforms model-held promises.
- [ ] **A wake that recognises.** A "since you last woke" section built from
      the agent's own commits, writes and changed self files, plus the age of
      each self file with an alarm at 90 days unchanged. (Handoff letters
      are already shown once, then archived: done 2026-11-04.)
- [ ] **Living interests.** Interest records backed by episodes
      (`mind/interests/events.jsonl`, a `smriti_interest` tool), with
      activation, phases and dormancy, rendered in a wake section. Interests
      without evidence show as "declared". Loves are a rendered view, never
      a free-text claim.
- [ ] **The self-change protocol** (plan 7.3: layers E0-E4, one
      `smriti_self_change` tool, cold re-reads, reaffirmation, floors as well
      as ceilings). Cross-review the spec with Codex before building it.
- [ ] Move `backup.py`'s run lock to `smriti.core.oslock` (an OS-level lock,
      released on crash), as the handoff lock already is.
- [ ] Isolate the Codex fallback seat (dedicated `CODEX_HOME`, no AGENTS.md,
      hooks, plugins or MCP), as the Claude seat already is (plan 0.4).

## Hardening (from two no-context code reviews, 2026-11-04: C+ and B-)

Two fresh reviewers with no project context read the code. They agreed the
live core is sound (connection discipline, capture design, backup locking,
subprocess hygiene) and that these items stand between it and an adoptable
product, most important first:

- [x] **Path validation on every MCP tool that takes a path argument**
      (`smriti.core.safe_names`, applied centrally in the dispatcher;
      refusals are marked `isError`). Done 2026-11-05.
- [x] **The memory root's backup privacy:** a template `.gitignore`, queries
      logged as hashes, a wider scrubber, the private-store key in
      `private/.key` (never committed), docstrings that don't overclaim.
      Done 2026-11-05.
- [x] **Concurrency:** the day-log writer lock, MCP read-modify-write
      handlers, `index.md` maintenance and the journal append hold OS locks
      (`smriti.core.oslock`, `smriti.core.files`); the witness clears
      `.git/index.lock` only when no git runs and the lock is unchanged; the
      index records each source's mtime and size as read, a full run commits
      in one transaction, and a read wins only when it is the file as it is
      now. Done 2026-11-05 (hardening H1).
- [x] **No silent overwrites:** same-day, same-title findings and decisions
      take `-2`, `-3`... Done 2026-11-05 (H1.7).
- [ ] **Types that match the code:** `ensure_schema` returns `IndexDB`, not
      `sqlite3.Connection`; `search()` takes an `IndexDB`. Then work the
      `mypy --strict` count down. The `mcp_server.py` docstring still says
      "two tools".
- [ ] **Personal coupling in the logic, not just the names:** speakers are
      typed `"suti" | "narada"` and the human/autonomous split keys on
      `who == "suti"` (`daylog/model.py`, `digest.py`, `render.py`); make
      roles generic (user / agent) with names from config (plan 6.3).
- [ ] **Digest truncation:** chunk long days instead of cutting at 60k chars,
      or at least record the cut (plan 3.2).
- [ ] **Remove the parked pipelines** (plan 4.0, 6.4); split `mcp_server.py`
      into handler modules with a typed dispatch table; narrow the broad
      `except Exception` handlers; deduplicate the index insert loop.
- [ ] **CI:** ruff, the tests (with a cached or stub embedder), and strict
      mypy on touched files.

## Nightly cycle (spec: .ai/features/nightly-cycle.md) — top of stack

- [x] Capture core: per-source adapters (claude_jsonl incl. Telegram,
      codex_rollout, voice_md), stable turn ids + dedup writer,
      per-burst writer lock, event-time (+10) day placement, secret
      scrub, per-source scan-recency health.
- [x] `smriti nightly`: reconcile sweep -> repair pass (changed dates
      any age + 7-day scan) -> render -> no-tools digest
      (claude_cli primary, codex_cli fallback) -> capped closed-period
      rollup scan -> reindex -> `.nightly-status.json`.
- [x] `smriti morning`: fail-closed sent ledger, web-search-only
      compose, delivery via `notify_cmd` shim (the future
      presence-routing tool's first call site).
- [x] `smriti logd` daemon + `--ensure` keepalive; `smriti tasks
      install` (Task Scheduler fallback; migrate to Hermes cron once
      its cron home is verified live).
- [x] Presence line in the recall hook; WHILE YOU SLEPT in the wake
      briefing; day-log health in `smriti status`.
- [x] Deploy (2026-09-05): `smriti tasks install` (logd keepalive /10min,
      nightly 03:00, morning 07:00), queue.json archived to
      queue-archive-2026-09.json (274 tasks, reversible), logd live —
      49 days backfilled into ~/.narada/log/. NOTE: scheduled tasks run
      the working tree (pip -e); branch nightly-cycle is deployed but
      unmerged — merge to master is Suti's call.
- [x] Wire `notify_cmd` (2026-09-06): standalone stdlib sender at
      ~/.narada/.smriti/hooks/notify_telegram.py (bot token + chat id
      read at send time from ~/.hermes/.env, prana-alerts pattern —
      no dependency on the Hermes process). Live-verified: test send +
      first real morning message delivered. Branch merged to master
      and pushed same day.
- [ ] Nightly index step hit "database is locked" on its first run
      (2026-09-06 03:00) — a live session's MCP server holds index.db
      overnight. Add a busy_timeout / retry to `index_tree`'s
      connection, or make nightly retry the step once after a delay.
- [ ] Add prana-brain/phone-app transcript dirs to daylog.json sources
      when that work lands.

## Future introspection (parked 2026-09-04 — Suti's ruling: the deep
## pipeline was too ambitious to run at all; revisit only after the
## nightly cycle has run reliably for a sustained stretch)

- The cognitive cascade, ingest routing, batch consolidation, concept
  synthesis, and the entire "Active" research backlog below run only
  via `smriti sleep --deep` (manual). Nothing schedules them.

## Model & harness agnosticism

- [x] **Phase 9: install-script harness parity.** Three fixes that
      surfaced after the live Codex install:
      (1) `install_wake_files` now upgrades smriti-managed wake.py
      shims in place. Detection rule: first 400 bytes contain
      "smriti". Stale templates get a `.pre-upgrade.bak` and the
      new shim. Hand-customized files (no marker) are left alone
      with a warning. Smoke-tested across fresh / identical /
      stale-managed / hand-customized cases.
      (2) `scripts/setup_project.py` reworked: new
      `--harness {claude_code|codex|both}` flag (default both),
      Claude-Code auto-memory junction guarded behind that flag
      AND `~/.claude/` existing, latent `Path.is_junction()` bug
      fixed via a real reparse-point check, POSIX symlink fallback
      added. Smoke-tested across all three harness modes.
      (3) New `project_template/AGENTS.md` sibling to CLAUDE.md
      so Codex projects get a project-level override that
      delegates the smriti memory contract to the user-global
      `~/.codex/AGENTS.md` (Codex concatenates them root-first).
      `setup_project.py` copies CLAUDE.md, AGENTS.md, or both per
      `--harness`. Tests: 117 passed.
- [x] **Phase 8: Codex CLI adapter + shared integration module.**
      New `src/smriti/integrations/common/`: `mcp_spec.py` (single
      source of truth for the MCP server invocation +
      `make_wake_hook_command(framing)`), `agent_md.py`
      (`compose_agent_doc(addendum, memory_rel, header)` for both
      CLAUDE.md and AGENTS.md), `hook_scripts.py` (idempotent
      deployment), `wake_runner.py` (extracts the env-gating + backup-
      fire + briefing-emit logic; supports `raw` and `codex-json`
      framings via `SMRITI_WAKE_FRAMING`). `narada/.smriti/wake.py`
      slimmed to a 13-line shim that delegates to `wake_runner.run`.
      Claude Code adapter refactored to use the shared helpers; tests
      pass. New `src/smriti/integrations/codex/install.py` patches
      `~/.codex/config.toml` (TOML via `tomllib` + `tomli-w`):
      `[features] codex_hooks = true`, `[[hooks.SessionStart]]` ->
      wake.py with codex-json framing, `[mcp_servers.smriti]`. Drops
      `~/.codex/AGENTS.md` (under the 32 KiB cap). Force-injection
      via the SessionStart hook means identity loads at protocol
      level — agent has no choice to skip. `--harness codex` wired
      into `scripts/install.py`; `tomli-w` added to a new `[codex]`
      extras group. USAGE.md gets a Codex section. Idempotence
      verified end-to-end against a tempdir.
- [x] **Phase 8b: Codex PostToolUse parity.** Refactored
      `smriti.recall.hook` around per-tool `_extract_paths` so
      Claude Code's `Read|Edit|Write` (using `tool_input.file_path`)
      and Codex's `apply_patch` (parsing `tool_input.command` for
      `*** Add/Update/Delete File: <path>` directives, multi-file
      patches deduped by source) share the same recall pipeline.
      Bash and `mcp__*` tools intentionally skipped (Bash is too
      noisy; MCP would recurse on `smriti_read`). Output framing
      configurable via `SMRITI_RECALL_FRAMING={raw|codex-json}`,
      mirroring the wake-runner pattern. Codex installer wires
      `[[hooks.PostToolUse]]` matching `Edit|Write|apply_patch`
      pointing at deployed `~/.codex/hooks/recall_hook.py`. New
      `tests/test_recall_hook.py` (17 tests) exercises hook plumbing
      with synthetic stdin payloads — proves Codex parity works
      without needing a Codex subscription. Verified end-to-end
      against the live qmd index.
- [x] **Phase 1: trunk-distance reranker + qmd-skill quality fold-in.**
- [x] **Phase 2: LLM provider abstraction** — new `src/smriti/llm/`
      package with `LLMProvider` protocol, factory, four shipped
      providers (anthropic_api with prompt caching, claude_cli,
      openai_api, ollama). Auto-detect prefers anthropic_api when
      `ANTHROPIC_API_KEY` is set, else claude_cli. Override with
      `SMRITI_LLM_PROVIDER`. `api_backend.call_api` and
      `judge._call_claude` kept as thin shims so the 7 existing
      call sites don't need touching.
- [x] **Phase 3: repackage harness-specific hooks** — moved
      `src/smriti/hooks/{associative_recall,precompact_capture}.py`
      into `src/smriti/integrations/claude_code/hooks/` via
      `git mv` (history preserved). Added
      `src/smriti/integrations/_template/` with a docstring spec for
      adding new harnesses. `scripts/install.py` HOOKS_SRC updated.
      `docs/INSTALL.md` reference updated. `grep` for
      `~/.claude` / `claude_code` returns zero hits in
      `recall/`, `llm/`, `store/`. Settings/CLAUDE.md
      patching stays in `scripts/install.py` for now — Phase 4
      moves it into `integrations/claude_code/install.py`.
- [x] **Phase 4: split install scripts** — extracted core install
      into `src/smriti/install/core.py` (memory template, wake
      files, mirrors, qmd daemon) and Claude-Code install into
      `src/smriti/integrations/claude_code/install.py` (hook
      deployment, settings.json patch, MCP registration, CLAUDE.md).
      `scripts/install.py` is now a thin dispatcher with
      `--harness {claude_code|none}` (defaults to claude_code for
      back-compat). `--harness=none` runs core only — verified end-
      to-end. New harnesses register via
      `smriti.integrations.<name>.install`.
- [x] **Phase 5: AGENT.md generic + wake-as-library** —
      `src/smriti/templates/AGENT.md` ships with the package and
      carries the harness-neutral memory contract; new "When to call
      `smriti_read`" section closes the agent-initiated-recall gap.
      `~/.claude/CLAUDE.md` is now composed at install time from
      AGENT.md + a small Claude-Code-specific addendum (SessionStart
      hook details, SMRITI_WAKE gating). `smriti.wake.briefing()`
      extracted as a pure-assembly library function so non-Claude
      harnesses can build the same identity payload;
      `narada/.smriti/wake.py` slimmed from 322→90 lines as a thin
      wrapper around it. `examples/python_agent.py` updated to
      compose its system prompt from AGENT.md + `briefing()` —
      validates the library contract end-to-end (14k char prompt
      assembled from package data + memory tree, no Claude-Code
      coupling). Tests: 100 passed, 1 pre-existing unrelated.
- [x] **Phase 6 (sketch):** `examples/python_agent.py` —
      smoke-tested end-to-end. Validates that Phase 1+2 library
      APIs work without any harness; surfaced wake-as-library as
      the only remaining gap. Serves as a regression check —
      anything in Phases 3-5 that breaks library agnosticism
      breaks this example.
- [x] **Phase 7: close the gaps Phase 6 surfaced.**
  - [x] Multi-turn conversation: added `Message` dataclass and
        `LLMRequest.messages: list[Message] | None`. All four
        providers consume `request.turns()` — Anthropic and OpenAI
        and Ollama natively, claude_cli renders Human/Assistant
        markers (no `--resume` yet). `call_llm` accepts
        `messages=`. Example threads history.
  - [x] Tool-wiring demo: new `smriti.recall.wrap_tool(fn,
        query_from=...)` returns a callable that bundles
        `ToolResult(output, recall, recall_block)`. Example wires
        `/tool read` and `/tool ls`; recall block is appended to
        history so the next turn sees ambient memory.
  - [x] Auto-journal on session end: `_auto_journal` writes a
        turn-by-turn summary to `journal` branch on exit / Ctrl-D.
        Demonstrates write side of the library.
  - [x] `/provider list`: new `smriti.llm.list_providers()`
        enumerates registered providers with availability, default
        models, aliases. Wired to `/provider list` in the example.
  - [x] `docs/USAGE.md` — library entry points, four
        `SMRITI_LLM_PROVIDER` invocations with env var matrix,
        agent-building recipe with composition snippets. Linked
        from `INSTALL.md`.

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

Memory-derived actionables that used to sit here (extracted by the parked deep
pipeline from private memory threads) were moved out of the public repo on
2026-11-04. Current work: `.ai/features/smriti-renewal.md`.

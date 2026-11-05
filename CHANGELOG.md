# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The package version remains 0.1.0; subsequent development is unreleased.

## [Unreleased]

### Added

- Deterministic, dependency-free `stub` embeddings and a `needs_model` test marker
  for model-free CI runs.
- GitHub Actions lint and tests on Windows, Linux, and macOS with Python 3.11/3.12.
- Security policy, contribution guide, this changelog, and an MIT licence.
- Claude Code and Codex integrations, managed agent instructions, a shared hook
  model, and configuration diagnostics.
- Conversation capture, nightly digests and rollups, morning messages, and
  OpenTimestamps witness records.
- A wake briefing library, archived session handoffs, and an explanation of the
  self model in `docs/SELF.md`.
- A per-user embedding service (`smriti embed serve|status|stop`): one model per
  memory root, shared by every session's MCP server and `smriti read` over
  mutually authenticated loopback TCP, started on demand and exiting when idle.
  An MCP server no longer loads a model (44 MB measured, against about 1 GB).
  `SMRITI_EMBED_SERVICE=0` turns it off.
- The embedding identity (provider, model, model revision, dimension), recorded
  with an index's first vectors and checked wherever vectors are written or
  compared. Writes without vectors are indexed for keyword search and their
  vectors filled later; reads, writes and status say when they are degraded.

### Removed

- The parked deep-sleep pipeline (cognitive cascade, JUDGE, ingest and routing,
  consolidation, threads, actionables, journal rollups, the wake-context rebuild),
  its task queue, file watcher and daemon, and the `sleep`, `queue`, `daemon`,
  `watch`, `ingest` and `merge-concepts` commands. `smriti eval` keeps its search
  cases. The code is in the git history.

### Changed

- The MCP server is a package, `smriti.mcp`: a `Tool` table in place of an
  if/elif chain, and one error model (a handler raises `ToolError`; every failure
  is an `isError` result). `python -m smriti.mcp_server` still works.
- A long day is digested in up to four time-ordered parts and a merge call, and
  any cut is recorded in the prompts and the digest's frontmatter.
- `index.md` upkeep after a write lives in `smriti.store.index_pages`.
- Evaluation uses a packaged synthetic corpus in a disposable sandbox; tests
  isolate memory roots and metrics from the real tree.
- Documentation describes current behaviour, distinguishes parked pipelines, and
  generates CLI, MCP, and test counts from code with a drift check.
- Writes no longer feed the parked task queue by default. The nightly no longer
  refreshes qmd; smriti's own index is the default recall backend.
- Search uses bounded rare-term FTS queries and consistent keyword-only scoring.
  Indexing shares one exclusion policy and purges deleted or excluded sources.
- Search telemetry logs query hashes and lengths by default. The memory template
  ignores runtime state and the private-store key; shipped examples use synthetic
  data in place of personal details.
- Backups run hourly from a `smriti-backup` scheduled task (`smriti backup
  --push`), not at every session start and end; a run with nothing new makes no
  network call. The nightly and morning tasks run windowless.
- Capture is off until `.smriti/daylog.json` turns it on: `"capture": true` for
  the default sources (the installer's `--capture` writes it), or a `"sources"`
  list. With no file, or one with neither key, nothing is captured, where every
  Claude Code transcript and Codex rollout on the machine was; `smriti logd` and
  `smriti status` say why and how to turn it on. An existing install that relied
  on the defaults needs `"capture": true` added; an installer rerun writes it
  where the installer recorded a "yes".
- `smriti_write` and `smriti write` mask credential-shaped strings, as capture
  does, before an entry is saved.

### Fixed

- MCP path-argument traversal and Windows device aliases; write branches escaping
  the memory tree.
- Private-store writes colliding, keys committed beside ciphertext in new stores,
  existing ignore rules being lost, and accidental replacement of missing keys.
- More credential patterns are masked during day-log capture.
- Backup serialisation, stale-lock rechecks, detached hook execution, and unsafe
  termination of git operations; handoff operations now use an OS-level lock.
- Windows PID probes sending console interrupts, ONNX integer input types,
  provider selection for existing indexes, read-only SQLite access, and index
  transaction cleanup after failures.
- Yearly rollup input budgeting and isolation of internal Claude CLI calls.
- Concurrency: the day-log writer lock, the MCP read-modify-write handlers,
  `index.md` upkeep and journal appends hold OS-level locks; the witness clears a
  git lock only when no git runs; the incremental index tracks each source's
  mtime and size as read; a full re-index commits in one transaction; and a
  writer's read replaces another's only when it is the file as it is now.
- Same-day, same-title findings and decisions no longer overwrite each other.
- A write branch's segments follow the same rule as every other path argument
  (no `.git`, `.smriti/x` or device names); MCP arguments are checked against
  each tool's input schema (`smriti_read`'s `top_k` is 1 to 50); `smriti status`
  opens the index read-only; a feature moved to shipped or abandoned is reported
  as moved.
- A search no longer waits out its 3 s deadline while the embedding service is
  still loading its model; it falls back to keywords at once.
- A failed index run reports its real error (the wrapper had no `rollback`).
- A malformed MCP message gets an error instead of ending the server.
- Session channels are named correctly from a cwd of any platform.
- Blank Windows Terminal windows taking focus: the backup worker, the logd and
  qmd daemons, and the LLM CLI calls give their console children a hidden console
  instead of none.

## [0.1.0] - 2026-04-15

The initial release is tagged `v0.1` in git.

### Added

- Markdown memory writes, a local SQLite hybrid search index, and MCP access.
- Ingestion and routing, structural and cognitive cascades, and cycle-safety
  checks. These pipelines were part of the initial release; several are now parked.

[Unreleased]: https://github.com/Stormbane/smriti/compare/v0.1...HEAD
[0.1.0]: https://github.com/Stormbane/smriti/releases/tag/v0.1

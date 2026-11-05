# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-11-06

Since 0.1.0 the parked pipelines are removed, the maintainer's own setup is an
optional profile in its own package, and capture is off until configured.
A pre-0.2 install keeps its `~/.narada` root; one with the reference profile
also needs `pip install -e profiles/reference`.

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
- Profiles are installed packages, found through the `smriti.profiles`
  entry-point group, not subpackages of `smriti.profiles`. A missing, ambiguous
  or broken profile is a `ProfileError` and costs only its own tools and wake
  sections; the morning still refuses to send.
- The reference profile (the maintainer's installation) is its own
  distribution, `smriti-reference` in `profiles/reference/`, and smriti's wheel
  carries none of its code. That installation needs `pip install -e
  profiles/reference` beside smriti to keep its profile.
- The default memory root is `~/.smriti`. With no `SMRITI_ROOT`, an existing
  `~/.narada` (the default until now) is still used, even beside a `~/.smriti`;
  `smriti status` shows the root and the rule that chose it, and warns when both
  exist. Every former hard-coded `~/.narada`, including the recall hook's
  "don't recall the tree into itself" check, follows that root.
- The digest and rollup calls run `claude -p` with no tools, MCP servers, skills
  or slash commands (`--strict-mcp-config --tools= --disallowedTools=*
  --disable-slash-commands`); `tests/test_llm_tool_boundary.py` checks them
  against the installed CLI when `SMRITI_LIVE_CLI_TESTS=1`. The morning's web
  search is opt-in, `"morning_web_search": true`; with it on, WebSearch is the
  call's only tool. PRIVACY.md says what an injected instruction can still do.
- Backups push the current branch to its upstream, or to `origin` under the
  same name, not a fixed `master`. The installer's privacy question is about
  that destination, resolved through git's `pushInsteadOf` and `insteadOf`
  rewrites. A deployed `<root>/.smriti/backup.py` needs the installer rerun.
- smriti's named locks (file edits, the day-log writer) live in the private
  per-user runtime directory. While processes of an earlier release may still
  run, each also takes its old file in the shared temp directory;
  `SMRITI_LEGACY_LOCKS=0` stops that once none can.
- The `read` extra's model download (`gpahal/bge-m3-onnx-int8`, about 560 MB)
  and `HF_HUB_OFFLINE` are documented, with the other network requests, in
  README and PRIVACY.md.

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
- `pip wheel .` builds again: the eval corpus was added to the wheel twice, which
  current hatchling refuses.
- A malformed MCP message gets an error instead of ending the server.
- Overlapping morning runs send one message: a run claims the day before it
  composes, and a stale claim is taken over after 30 minutes.
- `smriti_write` and `smriti_record_finding` no longer describe the removed
  JUDGE step and cascade.
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

[Unreleased]: https://github.com/Stormbane/smriti/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Stormbane/smriti/compare/v0.1...v0.2.0
[0.1.0]: https://github.com/Stormbane/smriti/releases/tag/v0.1

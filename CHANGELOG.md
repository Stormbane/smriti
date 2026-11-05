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

### Changed

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

## [0.1.0] - 2026-04-15

The initial release is tagged `v0.1` in git.

### Added

- Markdown memory writes, a local SQLite hybrid search index, and MCP access.
- Ingestion and routing, structural and cognitive cascades, and cycle-safety
  checks. These pipelines were part of the initial release; several are now parked.

[Unreleased]: https://github.com/Stormbane/smriti/compare/v0.1...HEAD
[0.1.0]: https://github.com/Stormbane/smriti/releases/tag/v0.1

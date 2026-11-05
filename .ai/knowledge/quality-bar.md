# The quality bar, and the outside-review practice

## The practice

Every significant stretch of work ends with an **outside review**: a fresh
agent with no project context is pointed at a fresh clone of the public repo
and asked, as an engineer deciding whether to adopt it, to grade the code from
the code itself (not from the README's claims), with file-and-line evidence.

- **Isolation is part of the method.** The reviewer runs with a temporary
  config: no user CLAUDE.md, hooks, MCP servers, skills or auto-memory, an
  empty home, and deny rules for everything outside the clone. A canary run
  proves it before any real run. A review that has seen our context isn't an
  outside review.
- **Two models**, so one model's taste isn't the verdict.
- **Findings become work.** They go into `.ai/todo.md` "Hardening", most
  important first, and the next review checks whether the grade moved.
- **Grades are recorded** below, so the trend is visible.

The harness currently lives with the private audit notes, because it carries
machine-specific paths. Generalising it into a `scripts/` tool is on the list.

## What an A+ requires

Derived from what the reviewers graded on: architecture and module
boundaries, readability, correctness and error handling, concurrency and data
safety, testing, security and privacy, and packaging and maintainability. An
A+ means a skeptical reviewer finds **no P1 or P2 issue** and would adopt it
as a dependency.

1. **Security and privacy.**
   - Every input boundary is validated: MCP arguments (done), CLI arguments,
     configured paths.
   - Nothing secret or personal is logged by default (query logging done).
   - Privacy defaults are safe out of the box: a template `.gitignore` and the
     private key out of git (done).
   - A short threat model and a `SECURITY.md` explain how to report issues.
2. **Correctness and data safety.**
   - Every lock is OS-level (`smriti.core.oslock`) or token-checked; none is
     broken by age while its owner lives.
   - Every write that matters is atomic or appended under a lock.
   - The index rebuilds into a new generation and swaps, so search is never
     empty mid-rebuild.
   - The digest never silently drops part of a day.
3. **Architecture.**
   - No dead code: the parked pipelines are removed, not just switched off
     (done: hardening H3).
   - `mcp_server.py` is split into handler modules with a typed dispatch table.
   - Modules stay small and cohesive.
   - The core is generic; the reference user's profile (presence, messaging,
     persona) is an optional plugin with one config system.
4. **Typing and errors.**
   - `mypy --strict` is clean (about 209 errors to go); `ruff` already is.
   - There is one error model, and broad `except Exception` handlers appear
     only at process boundaries, each with a stated reason.
5. **Tests.**
   - Fast by default, with a stub embedder, and the real one marked slow.
   - Coverage is measured, and high on the core.
   - Concurrency and crash tests cover every lock and every atomic write.
   - Property-based tests cover capture and deduplication.
   - An install-from-wheel test proves a stranger's first five minutes work.
6. **Release discipline.**
   - CI runs on Windows, Linux and macOS, with a badge.
   - Releases are versioned, with a changelog, and the wheel includes its
     templates.
   - There's a `CONTRIBUTING.md`.
7. **Docs.**
   - They stay accurate: the comprehension checks pass, and the generated
     counts can't drift.
   - An as-built architecture document replaces the April design notes.
   - The examples run.

A realistic path:
- **B+** once the Hardening list in `.ai/todo.md` is done;
- **A** once mypy is clean, CI runs, the parked code is gone and the MCP
  server is split;
- **A+** when an outside reviewer can't find a P1 or P2 and would adopt it as
  a dependency.

## Grades

| Date | Commit | Opus | Sonnet | Notes |
|---|---|---|---|---|
| 2026-11-04 | `d5543b6` | C+ | B- | First outside review. Blockers: MCP path validation, backup privacy, concurrency, digest truncation, parked code, no CI |
| 2026-11-05 | `4c9c9dd` | C+ | B- | After fixing MCP path validation and backup privacy, both now cited as strengths. The grade holds on what's left: the day-log lock and unlocked read-modify-write handlers, personal coupling in the logic (speaker types, persona tools), parked code on the write path, type annotations that don't match the code, no CI |
| 2026-11-05 | `3074a41` | B- | B | After hardening H1-H7. Now cited as strengths: OS-level locks and the indexer's race handling, path safety at the protocol boundary, crash-safe capture, isolated LLM subprocesses, CI that matches its claims. Held down by: plaintext transcripts pushed to a remote by default, the CLI read path taking a write lock, search weights when the vector leg fails, an unguarded scanner `stat`, unescaped frontmatter, a UTC+10 default day boundary, strict types and lint stopping short of the CLI, llm, integrations and private |
| 2026-11-05 | `f1319a1` | B | B+ | After H9. Now cited: the concurrency and crash safety, the tested input boundary (path safety, frontmatter folding, isError), visible failures, strict types and pinned tools on three OSes, the private store. Held down by: capture's broad default without the installer, an unverified no-tools flag and WebSearch on LLM calls fed untrusted text, a morning double-send race, a hard-coded `master` push, the reference profile in the wheel, the `~/.narada` default root, no tagged release |
| 2026-11-06 | `bcec9a2` | B- | B+ | After H10 and H11, at v0.2.0. Now cited: the OS locks and their tested transition, the indexer's injected-race tests, the embedding service's IPC (mutual HMAC, bounded, loopback), the input boundary, the CI wheel check, an honest SECURITY.md. Held down by (Opus, all verified): a wheel install can't run its installer (templates found outside the package; a `C:/Projects` default), credential masking missing the project tools and the handoff, non-ASCII query terms stripped from keyword search, porous module boundaries (private imports, provider precedence written twice and differently, version and description drift), the codex fallback reading untrusted transcripts with a shell and an unmasked digest. Sonnet: broad excepts without the convention's reason, regex-only masking, one maintainer, the shape of one setup, incident-dense comments. Every problem #4 named is fixed; Opus's five are new, one layer deeper in the same themes (packaging, privacy, boundaries), and it graded a step lower |
| 2026-11-06 | `bcec9a2` | (B-) | Codex: C | From here the reviewers are Opus and Codex (gpt-6.1-sol, high reasoning effort), both on subscriptions, so no model family grades its own; Sonnet is retired. At its default (low) effort Codex gave B-. At high effort, C: beyond Opus's codex-fallback, torn day-log line, uneven masking and wheel install, it found (verified) that a transcript line over 8 MiB stalls capture of that file, that the nightly has no run lock and shares fixed `.tmp` names, that a digest hashes turns it didn't read, that rollup freshness ignores a regenerated digest's content, and that backup failures don't reach the nightly's health. Strengths: the profile seam, the indexer's consistency, kernel locks and the morning claim, the embedding service's authentication, tests beyond the happy path |

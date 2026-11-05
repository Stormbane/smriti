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
   - No dead code: the parked pipelines are removed, not just switched off.
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

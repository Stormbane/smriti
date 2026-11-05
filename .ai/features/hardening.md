---
status: in progress
opened: 2026-11-05
goal: an outside, no-context reviewer finds no P1/P2 issue and would adopt smriti as a dependency (see .ai/knowledge/quality-bar.md)
baseline: outside reviews at 4c9c9dd: Opus C+, Sonnet B-
---

# Hardening: from C+ to A

This plan works through the findings of two isolated, no-context code reviews
(`.ai/knowledge/quality-bar.md`, `.ai/todo.md` "Hardening"). Each phase:

- lands as atomic commits;
- adds tests that would have caught the problem;
- gets a Codex diff review, with its findings fixed and rechecked once;
- keeps the full suite and `ruff` green.

The plan ends with a third outside review.

Phases are ordered so that workstreams touching the same files never run in
parallel. H2 runs alongside H1 because it only adds new files.

## H1. Concurrency and data safety

| # | Change | Acceptance |
|---|---|---|
| H1.1 | `daylog/lock.writer_lock` uses `smriti.core.oslock`. The lock file sits in the temp dir, keyed by the lock path, never inside the tree. | Two processes are serialised. A holder killed with `os._exit` can't leave it stuck. A long holder (>300 s) isn't broken. The API (`writer_lock`, `LockTimeout`) is unchanged. |
| H1.2 | `witness.py` clears `.git/index.lock` only the way `backup.py` does: no git process running, and the lock unchanged since it was inspected. Never by age alone. | Tests: a lock is kept while git runs, kept if replaced, and cleared when stale and idle. |
| H1.3 | Read-modify-write edits of shared files run under one tree lock (oslock `tree-edit`): `add_todo`, `resolve_outbox`, the `record_*` index updates, and `cascade.structural_cascade`'s `index.md` rewrite. | A concurrent-writer test loses no line. |
| H1.4 | `writer._append_entry` appends under an oslock keyed by the file. | 16 threads appending to one daily file produce 16 intact entries. |
| H1.5 | The incremental index compares a file's mtime with the mtime it recorded for it (a `sources` table), not with the time it was indexed. | A file edited while it is being indexed is re-indexed on the next run. |
| H1.6 | `index --full` deletes and re-inserts in one transaction, committed at the end. Readers keep the old index until then, and a crash rolls back. | A test interrupts a full rebuild, and the old index stays searchable. |
| H1.7 | `record_finding` and `record_decision` never overwrite: a same-day, same-title entry gets a `-2`, `-3` suffix. | Test. |

## H2. CI, a fast test embedder, project hygiene (new files only; parallel with H1)

| # | Change | Acceptance |
|---|---|---|
| H2.1 | A deterministic `stub` embedding provider (hashed bag of words, fixed dimension), selected with `SMRITI_EMBED_PROVIDER=stub`. Tests that need a real model are marked `needs_model` and skipped when it's absent. | The full suite runs in CI with no model download. |
| H2.2 | GitHub Actions: `ruff check`, then pytest with the stub embedder, on Windows, Linux and macOS, Python 3.11 and 3.12. A README badge. | The workflow is green on push. |
| H2.3 | `SECURITY.md` (how to report, threat model, privacy defaults), `CONTRIBUTING.md` (setup, tests, the outside-review practice), `CHANGELOG.md` (0.1.0 and Unreleased). | Present and accurate. |

## H3. Remove the parked pipelines

| # | Change | Acceptance |
|---|---|---|
| H3.1 | Delete the deep-sleep pipeline and everything only it uses: `sleep --deep`, the cascade's cognitive part, judge, ingest, route, consolidate, threads, actionables, the journal-rollup pipeline, the task queue, the watcher, the daemon, `merge-concepts`, and their tests and CLI commands. The deterministic `index.md` link maintenance that `smriti_write` uses stays, under a name that says what it does. | `cli.py` shrinks by about 600 lines; no import of a removed module remains; the docs' counts are regenerated; the suite is green. |
| H3.2 | `smriti eval` keeps its search cases; the judge and cascade cases go with their modules. | `smriti eval` runs. |

## H4. MCP server structure and error model

| # | Change | Acceptance |
|---|---|---|
| H4.1 | Split `mcp_server.py` into `smriti/mcp/` (protocol, tool schemas, one module per tool group) with a typed dispatch table (name → handler) replacing the if/elif chain. | `python -m smriti.mcp_server` still works (shim); tool behaviour is unchanged (tests). |
| H4.2 | One error model: a handler raises `ToolError` for refusals, and the dispatcher turns any exception into `isError`. No "Error:" string protocol. | Tests for the error paths. |

## H5. A generic core; the reference user becomes a profile

| # | Change | Acceptance |
|---|---|---|
| H5.1 | Speaker roles are `user` and `agent` in the data model, with display names from config (`daylog.json`: `user_name`, `agent_name`). Existing day-logs (`who: "suti"/"narada"`) are read as user/agent. | Capture, render and digest key on roles; old logs still render identically. |
| H5.2 | The reference user's tools (`tell_suti`, `smriti_ask_suti`, `smriti_propose_goal_shift`, `smriti_resolve_outbox`), the wake's presence sections and the morning persona move into an optional `smriti.profiles.<name>` package, enabled by config. The core server lists only generic tools when no profile is set. | A fresh install exposes generic tools only; the reference install keeps everything (config). |

## H6. Digest coverage

| # | Change | Acceptance |
|---|---|---|
| H6.1 | Long days are digested in time-ordered parts (at most 4 parts of 60k plus one merge call); beyond that, the cut is recorded. The digest frontmatter records the characters seen and total, and whether anything was cut, and the prompt is told. | No silent truncation; a test on a synthetic 200k-char day. |

## H7. Types

| # | Change | Acceptance |
|---|---|---|
| H7.1 | Annotations match the code (`ensure_schema` → `IndexDB`; `search(conn: IndexDB)`, ...). The `mcp_server` docstring tells the truth. | `mypy --strict` clean on `smriti.core`, `smriti.store` (live modules), `smriti.daylog`, `smriti.wake`, `smriti.mcp`, enforced in CI for those packages. |

## H8. Outside review #3

Run the isolated two-model review on the public repo and record the grades.
New P1/P2 findings become H9.

## H9. From outside review #3 (B- and B, at `3074a41`)

Both reviewers verified their claims against the code; the first five
were rechecked by hand before this list was written.

| # | Change | Acceptance |
|---|---|---|
| H9.1 | `smriti read` opens the index with `open_readonly`, never `ensure_schema` (which writes). | A test: the CLI read holds no write lock. |
| H9.2 | When the vector leg fails, search rescales as in keyword-only mode. | A test: scores keep the 0-1 scale when embedding raises. |
| H9.3 | The scanner skips a file deleted between the walk and its `stat`. | A test: a vanishing file doesn't abort `index_tree`. |
| H9.4 | Frontmatter values (`title`, `source`, `tags`, and the project tools' fields) can't forge keys: newlines are folded and values quoted where needed. | Tests with injected newlines. |
| H9.5 | The day boundary defaults to the system's local time zone (DST included); `SMRITI_DAY_UTC_OFFSET` and a daylog.json `timezone` override it. Journal writes and the day-log use the same clock. | The reference install sets its zone explicitly; tests for both. |
| H9.6 | The CLI has a top-level handler: a clean message and exit code, the traceback only with `-v`. | A test. |
| H9.7 | Small robustness: `private/.key` is created mode 600 on POSIX; the todo dedup matches a whole topic, not a substring; a missing `mirrors/` gives a useful error; the MCP server reopens its index when the root changes. | Tests. |
| H9.8 | `mypy --strict` extends to `cli`, `llm`, `integrations`, `private`, `recall`, `eval` and the rest of the package; CI checks all of `src/smriti`. | CI. |
| H9.9 | Ruff adds `E501` (line length 100), `B` and `BLE` (blind excepts, each kept one justified with a reason). `backup.trigger` and the reranker log their failures. | CI. |
| H9.10 | One insert path for `index_tree` and `index_file`. | The suite. |
| H9.11 | Capture privacy is chosen, not defaulted: the installer asks whether to capture transcripts and whether a backup remote is private, and says what is pushed in the clear; the README's privacy section leads with it. | Install test; README. |
| H9.12 | Outside review #4. | Grades recorded. |

Considered and kept: `backup.py` keeps its own copies of `pid_alive` and
`git_running` (it is a standalone script copied into the memory root and
must run without smriti importable); the reference profile ships in the
wheel as the worked example of a profile, off unless configured.

## Status

Updated as phases land. Each phase had a Codex diff review, fixes for every
substantiated finding with a regression test that fails without the fix, and
a recheck.

- **H1 done** (2026-11-05). Review: four rounds, four real findings, all in
  the index's concurrency (a full run's deferred delete wiped concurrent
  writes; a later commit is not a later read, twice; index_file writes out
  of order). The rule that held: a read wins only when it is the file as it
  is now. Writing the tests also found `IndexDB.rollback` missing, which had
  hidden every failed index run's real error.
- **H2 done** (2026-11-05; Codex implemented, Claude reviewed). CI's first
  runs found what the development machine hid: ruff's default rule set
  changing between releases (now stated and pinned), an undeclared
  `watchdog`, Windows-only path parsing in the capture adapters, a
  newline-shaped test bound, and macOS's sqlite3 unable to load sqlite-vec.
  Green on all six jobs since.
- **H3 done** (2026-11-05). `cli.py` 1,670 → 581 lines; the package 22k →
  15k. Review: no findings. prana's dormant heartbeat still calls the removed
  `smriti ingest` and `smriti sleep`; nothing runs it.
- **H4 done** (2026-11-05). Review: one finding (a malformed tool name ended
  the server), fixed; recheck clean.
- **H5 done** (2026-11-05). Speakers are roles (user, agent) with names from
  daylog.json; turn ids and input hashes are unchanged (the old words are
  hashed for the roles), and an old day renders byte for byte as before. The
  reference user's four tools, the wake's presence sections and the morning
  persona are the `reference` profile, on in the maintainer's install by
  `"profile": "reference"`. Review: no findings.
- **H6 done** (2026-11-05). Review: an unbounded marker trailer, then a lone
  `\r` that let a heading in the last turn pose as the trailer; both fixed.
- **H7 done** (2026-11-05; Codex typed core and store, Claude the rest).
  CI runs `mypy --strict` on core, store, daylog, wake, mcp and profiles on
  every OS, with mypy pinned; the first Linux run found Windows-only code
  behind `os.name`/`hasattr` checks that mypy doesn't narrow.
- **H8 done** (2026-11-05). Review #3 at `3074a41`: Opus B- (was C+), Sonnet B
  (was B-). The findings are H9.
- **H9.1-H9.11 done** (2026-11-05). Codex reviewed each branch; real findings
  fixed with regression tests: a missing crypto extra left an empty key that
  blocked the store; the day zone ignored an explicit root; the installer
  missed a running logd (logd now follows daylog.json), trusted a git-crypt
  attribute without its filter, trusted ignore rules over tracked and waiting
  files, sampled too few log outputs and only one push URL. CI now runs ruff
  with E501, B and BLE, and `mypy --strict` on the whole package, on every OS;
  getting there found numpy's and the Anthropic SDK's stubs differing by
  version from the development machine's.
- **H9.12** (outside review #4): next.

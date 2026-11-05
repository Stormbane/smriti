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

## H10. One embedding model per machine, not one per MCP server

**Why.** Measured 2026-11-05: an MCP server is 19 MB until its first search
or write, then 1,028 MB, because `_get_embedding_provider()` loads the
embedding model in-process (the reference index uses the `local` provider,
MiniLM on PyTorch) and keeps it for the life of the process. Every open
session runs its own server, and harnesses that leak sessions multiply it:
66 servers held about 32 GB. The first search in each server also waits
about 14 s for the model.

**Design (rev 2, after the Claude-Codex cross-review; dispositions below).**
A per-user embedding service holds the model once; every long-lived
process asks it.

*Embedding identity.* An index is built with one embedding identity:
provider key, model name, model revision (the files' hash where the
provider exposes it), and dimension. It is recorded in the index's `meta`
atomically with the first vectors, including when the first vectors come
from `index_file` on an empty index. Every embedding result carries the
identity that produced it. `index_file` checks it against `meta` inside its
`BEGIN IMMEDIATE` transaction, and search against the snapshot it reads
vectors from; a mismatch is stale work: it is retried once on a fresh
embedding, then treated as the service being unavailable. This closes a gap
that exists today even without a service (a cached in-process model
outliving a migration).

*Transport.* Loopback TCP (`127.0.0.1`, an ephemeral port), stdlib
`socket` only. Every connect, read and write has a socket timeout.
Messages are length-prefixed JSON (never pickle), at most 4 MB a frame and
64 texts a request; clients split larger batches. Each connection is
authenticated both ways by HMAC challenge-response over a per-user key, so
a client can't be served by an impostor that bound the port after the
service died, and nobody without the key gets an answer. The listener
accepts on one thread and hands each connection to a worker with its own
deadline, so a stalled or hostile peer can't block others.

*Runtime directory.* Keys, the discovery file (port, pid, instance id),
the service lock and the service log live in a private per-user directory
outside the memory root: `%LOCALAPPDATA%\smriti\run` on Windows (the
profile's ACLs), `$XDG_RUNTIME_DIR/smriti` or `~/.cache/smriti/run`
elsewhere, created 0700 with its owner verified, refusing to use one that
another user owns or others can write. The key is created atomically (an
exclusive create, mode 0600) and never replaced while a service uses it.
Same-user processes are trusted; this is stated in SECURITY. (`oslock`'s
own lock files stay where they are in H10: see round 2, finding 1.)

*Bounded admission.* At most 32 connections at once; beyond that a new
connection is closed on accept, before any buffer or worker is allocated.
The handshake must finish within 1 s. Authenticated requests wait in a
bounded queue (64), and a request whose client deadline has passed is
dropped unrun. So a peer that floods or stalls costs bounded threads,
sockets and memory, and clients degrade promptly instead of hanging.

*Lifecycle.*
- One service per memory root, enforced by an OS lock in the runtime
  directory held for its lifetime.
- A client that can't connect starts `python -m smriti embed serve`
  (hidden console: `CREATE_NO_WINDOW`) and waits, bounded; a second
  starter loses the lock race and exits.
- Restart is decided by the index, not by a client. A service serves the
  identity it loaded; when `meta` records a different identity (a
  migration, a full reindex with another model), the service drains:
  stops accepting, finishes in-flight work, exits. The next client starts
  a fresh one. Clients with differing code reuse whatever service is
  running, so long-lived sessions of different versions can't replace each
  other in a loop.
- A protocol version is part of the handshake. A client with an
  incompatible protocol degrades, as if the service were down, and never
  stops it.
- Code updates rotate in at idle exit: the service exits after 15 minutes
  without a request (configurable), which returns the model's memory to
  the OS (unloading inside a long-lived process wouldn't: PyTorch rarely
  gives memory back). `smriti embed stop` drains it at once. A request that
  arrives as it exits gets EOF and retries once on a fresh connection.
- Deadlines end to end, covering connect, handshake, queueing and
  inference: a query 3 s, a write's batch 30 s. Inference is serialised;
  queries go ahead of write batches.

*Degraded operation is visible, never silent.*
- Search without vectors runs keyword-only, and `smriti_read` says so
  ("keyword-only: <reason>").
- A write whose vectors can't be made still indexes its chunks for
  keyword search at once, and records the source with its vectors pending
  (a `sources.vectors` flag). `smriti_write` reports "saved; indexed for
  keyword search; vectors pending (<reason>)".
- Pending vectors are filled by the service when it starts, by a repair
  loop that runs in the service whenever sources are pending (small
  batches, between requests, queries first), by the next successful
  `index_file`, and by any `index_tree` run. So recovery doesn't wait for,
  or depend on, a nightly task or a later write. `smriti status` shows
  the pending count and the service's state.

*Who uses it.* `search()` and `index_file()` in every long-lived process
(the MCP server, `smriti read`, hooks), through `_get_embedding_provider()`.
`index_tree()` (the nightly, `smriti index`) keeps an in-process model: it
embeds thousands of chunks in one short-lived process. Both go through the
identity checks. `SMRITI_EMBED_SERVICE=0` turns the service off.

| # | Change | Acceptance |
|---|---|---|
| H10.0 | Size it: memory and latency of MiniLM through ONNX vs PyTorch, and their vector agreement. | Numbers recorded here; they decide only the service's own runtime, not whether there is a service. |
| H10.1 | Embedding identity: defined, recorded with the first vectors, checked inside `index_file`'s transaction and against search's snapshot; `sources.vectors` and pending-vector repair. | Tests: a migration between embedding and commit (equal and different dimensions) is caught; first-write bootstrap persists the identity; a restart with another provider installed refuses to mix. |
| H10.2 | `smriti.embed`: runtime directory, key, lock, TCP service with mutual HMAC, framing and bounds, bounded admission and queue, per-connection deadlines, drain, idle exit, discovery, the repair loop. `smriti embed serve`, `status` and `stop`. | Tests: round trip; vectors identical to in-process; a wrong key is refused both ways; an impostor on the port is detected; stalled handshake and partial frames time out without blocking others; simultaneous cold starts give one service; idle exit racing a request; drain on an index identity change; mixed client versions coexist; a foreign-owned or world-writable runtime directory is refused; sustained unauthenticated connections stay bounded and clients still degrade within their deadline; a timed-out write followed only by reads ends with nothing pending. |
| H10.3 | Wire `_get_embedding_provider()` to the service in long-lived processes; degraded results and statuses through MCP and `smriti status`. | An MCP server stays under 100 MB after searches and writes, never importing `torch` or `onnxruntime` (test). Outage: a write is saved and keyword-searchable at once, the read says keyword-only, and vectors fill in on recovery without a nightly (tests). With the real model (`needs_model`): vector fidelity and first-search latency under 1 s with the service warm. |
| H10.5 | Docs: README (what runs), ARCHITECTURE, SECURITY (the endpoint, its key, the trust boundary), the off switch. | Doc facts regenerated. |

### H10.0 measurements (2026-11-05)

On the development machine (12 cores, an NVIDIA GPU, Windows 11, Python 3.12,
torch 2.5.1+cu121, sentence-transformers 4.1.0, onnxruntime 1.21.1), each
variant in a fresh process, embedding 151 chunks of this repository's docs.
RSS is resident memory after loading, then after embedding; peak is the
process's peak working set. The ONNX MiniLM is the model repository's own
export (`onnx/model.onnx`) at the same snapshot as the PyTorch weights
(`1110a24`), run with mean pooling and the CPU arena off.

| Runtime | Load | RSS loaded → after | Peak | One query (median) | 151 chunks |
|---|---|---|---|---|---|
| MiniLM, PyTorch, as the `local` provider runs it here (CUDA) | 15.3 s | 711 → 1,042 MB | 1,042 MB | 10.7 ms | 0.23 s |
| MiniLM, PyTorch, CPU only | 15.0 s | 622 → 942 MB | 942 MB | 17.6 ms | 4.9 s |
| MiniLM, ONNX, CPU | 0.55 s | 155 → 167 MB | 758 MB | 10.3 ms | 7.4 s |
| bge-m3 int8, ONNX (the `onnx` provider's default), for scale | 3.2 s | 889 → 905 MB | 4,499 MB | 136 ms | 218 s |

- **Agreement:** MiniLM through ONNX and through PyTorch give the same vectors:
  cosine at least 0.999999 on every chunk (largest difference in any
  component 2.5e-7), and identical top-5 neighbours for every chunk.
- **What it decides.** ONNX would make the service about 170 MB resident
  instead of about 1 GB, and start in half a second instead of 15, with the
  same vectors; it embeds a large batch more slowly on the CPU than PyTorch
  does on the GPU, which the service (short batches, queries first) barely
  feels. The service keeps the index's recorded provider for now: serving
  MiniLM through ONNX needs a provider with mean pooling (the vendored `onnx`
  provider pools by CLS, which is right for bge-m3 but not for MiniLM) and a
  different provider key, so it is a migration (`smriti index --full`), not a
  drop-in. Recorded for a later decision; nothing in H10 depends on it.
- **Found on the way:** the `local` provider can't load with `HF_HUB_OFFLINE=1`
  under transformers 4.57 (its tokenizer loader lists the hub id as a
  directory); online, it loads from the cache.

### Cross-review dispositions (round 1, Codex adversarial review)

1. *Bind every embedding to the index identity of its transaction* [high]:
   **accepted**. Embedding identity, checked inside the write transaction
   and against the search snapshot (H10.1).
2. *Any-client restart creates a version-skew restart loop* [high]:
   **accepted, differently**. Restarts follow the index's recorded
   identity, which all clients share, not any client's version; differing
   code rotates in at idle exit; an incompatible protocol degrades and
   never stops the service.
3. *A bounded startup wait does not bound an embedding call* [high]:
   **accepted**. `multiprocessing.connection` authenticates with blocking
   calls; the transport becomes loopback TCP with socket timeouts, own
   framing and mutual HMAC, per-connection deadlines and bounded frames and
   batches; queries ahead of write batches.
4. *Empty-index startup has no durable provider authority* [medium]:
   **accepted**. The identity is persisted with the first vectors, whoever
   writes them.
5. *Protected runtime storage and endpoint ownership* [medium]:
   **accepted**. A private per-user runtime directory with verified
   ownership; mutual authentication defeats squatting; `oslock` moves
   there too (H10.4).
6. *Fallback hides loss of recall and read-after-write visibility*
   [medium]: **accepted**. Keyword indexing at once, vectors pending and
   repaired without a nightly, degraded states reported by MCP and status.
7. *Benchmark MiniLM ONNX before committing to the service* (next steps):
   **partly rejected**. Benchmarked as sizing (H10.0), but ONNX alone still
   costs about 250 MB per process, multiplied by every session, so it
   doesn't replace the service.

### Cross-review dispositions (round 2, the single recheck; then implementation)

Codex: round-1 findings 1, 2 and 4 resolved at plan level; the ONNX partial
rejection sound (66 sessions at 250 MB is still about 16.5 GB).

1. *Changing lock paths breaks exclusion during mixed-version operation*
   [high]: **accepted**. Long-lived MCP servers and logd keep the old
   `oslock` paths after an upgrade, so old and new writers could both
   enter `edit_lock` or the day-log writer lock. Moving `oslock` comes out
   of H10; it becomes H11.9, with a transition that takes both namespaces
   until no old process can be running, and an old/new contention test.
2. *Per-connection deadlines don't bound unauthenticated resource use*
   [medium]: **accepted**. Bounded admission, a 1 s handshake, a bounded
   queue, expired requests dropped; a saturation test.
3. *Pending vectors can stay stranded after a live-service recovery*
   [medium]: **accepted**. A repair loop in the service while sources are
   pending; a test of a timed-out write followed only by reads.

## H11. From outside review #4 (B and B+, at `f1319a1`)

| # | Change | Acceptance |
|---|---|---|
| H11.1 | Capture is off unless configured: with no `daylog.json` sources, logd captures nothing and says so; `smriti_write` content is scrubbed like captured turns. | Tests; README privacy text. |
| H11.2 | LLM calls fed untrusted text: verify the digest's no-tools flags against the installed Claude Code (a test that fails if tools are reachable, when the CLI is present); the morning call drops WebSearch unless configured on. | Tests; docs state the residual risk. |
| H11.3 | The morning message is claimed by an exclusive create before composing, so overlapping runs send one. | A race test. |
| H11.4 | Backups push the current branch to its upstream, not a hard-coded `master`. | Tests with a `main` repo. |
| H11.5 | `_checked_branch` agrees with `safe_name` (no dot-prefixed segments); MCP handlers type-check their arguments (`ToolError` on a non-string, `top_k` bounded); `smriti status` reads the index read-only; "Moved" when a feature moves. | Tests. |
| H11.6 | The reference profile moves out of the wheel into its own package (`smriti-reference`, or a plugin directory); the default root is neutral (`~/.smriti`) with `~/.narada` honoured where it exists. | A fresh install has no reference code; the reference install is unchanged. |
| H11.7 | Release: tag `v0.2.0` with the changelog; the `read` extra's model download is disclosed (and `HF_HUB_OFFLINE` documented). | A tagged release. |
| H11.8 | Outside review #5. | Grades recorded. |
| H11.10 | A search falls back to keyword-only at once while the embedding service reports "starting", instead of waiting out its 3 s deadline on every query during a cold start. | A test with a starting service. |
| H11.9 | `oslock`'s lock files move from the shared temp directory to the private per-user runtime directory (H10), through a transition that holds both the old and the new lock until no process with the old code can be running. | An old/new-process contention test for `edit_lock` and the day-log writer lock. |

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
- **H9.12 done**: outside review #4 at `f1319a1`, Opus B (was B-), Sonnet B+ (was B).
  The findings are H11.
- **H10 done** (2026-11-05). Live: an MCP server is 42 MB after searches (was
  1,028 MB); one shared service holds the model (1,029 MB); a search with the model
  warm takes 0.18 s. Cross-review: two plan rounds (nine findings, all accepted, one
  differently; the ONNX benchmark kept as sizing), then a diff review (three P2s)
  and a recheck (a P1 test that could only fail on POSIX, a P2 drain-wait slip), all
  fixed with tests; CI on Linux then caught zombies counted as alive and the ML
  stack's stubs reached through huggingface_hub. Left for H11: during a cold start
  each search waits its full 3 s before falling back; it should fall back at once
  while the service says it is starting.
- **H11**: in progress. Done: H11.1-H11.5, H11.9, H11.10. Next: H11.6, H11.7, H11.8.
- **H11.1, H11.5, H11.10 done** (2026-11-06, merged at `498ce0e` after a Codex diff
  review; its two findings, the capture override and the metrics scrub, fixed). Capture is off unless `daylog.json` says
  `"capture": true` (the default sources; the installer's `--capture` writes it) or
  lists `"sources"`; logd and `smriti status` say why nothing is captured and how to
  turn it on; a malformed daylog.json, which captured everything, now captures
  nothing. `--no-capture` still writes `"sources": []`, which a logd running older
  code also reads as off. An installer rerun writes `"capture": true` where a "yes"
  was recorded but daylog.json left it to the old default. `smriti_write` content,
  title and source are scrubbed. Branch segments follow `safe_name`; MCP arguments
  are checked against each tool's schema (`top_k` 1..50); `smriti status` opens the
  index read-only; "Moved" when a feature changes directory. A query falls back to
  keywords at once while the service says "starting".
- **H11.2 done** (2026-11-06). Run against Claude Code 2.1.283: the old
  `--disallowedTools *` did empty the tool list, but the wildcard is undocumented, and
  18 skills and 54 slash commands stayed. The digest now gets `--strict-mcp-config
  --tools= --disallowedTools=* --disable-slash-commands`, values attached because
  `--tools` is variadic: missing its value it took the next flag (`--tools
  --strict-mcp-config` loaded the account's claude.ai mail and documents
  connectors). A `needs_cli` test runs the real CLI with each call's flags and a
  prompt asking for a file, a command and a URL: the digest has no tool, the morning
  only WebSearch. Morning web search is opt-in, `"morning_web_search": true`; off,
  the morning gets the digest's flags and a prompt with no headlines. PRIVACY.md and
  SECURITY.md state what remains: codex's read-only sandbox, and search queries as
  a way out.
- **H11.3 done** (2026-11-06). The morning claims its day by an exclusive create of
  the ledger entry before composing; a failed compose releases it, a claim 30 min
  old with no send is taken over under the entry's edit lock, and a run that lost
  its claim doesn't send. A barrier test of two overlapping runs sends once.
- **H11.4 done** (2026-11-06, merged at `d8375d1`; Codex diff review and recheck). The
  backup pushes the current branch to its upstream, or to `origin` under the same
  name when it has none, setting the upstream; `needs_push` compares against the
  same ref; a detached HEAD isn't pushed and logs `push_skipped`. Tests on `main`
  repos with a bare remote. Review (P1): the installer's privacy check still
  inspected `origin`, so it could confirm a private remote while backups went to
  another. It now resolves the destination by the backup's rule
  (`privacy.backup_remote`, a copy, since the script can't import smriti; a test
  holds them together on eight repositories and checks that a real push lands
  where the installer said), and keys the saved answer on that remote's name and
  URLs. Recheck (two P2s, fixed): an upstream that names a URL directly is resolved
  through git's `pushInsteadOf`/`insteadOf` rules (pushInsteadOf first, longest
  prefix), so the installer names, and keys its answer on, where git really pushes;
  and the remedy names every remote the backup can reach, `origin` included, since
  unsetting the upstream alone falls back to it. The copy deployed in each memory
  root (`<root>/.smriti/backup.py`) must be redeployed (the reference install's
  was, 2026-11-06). Merge review: a remote whose name holds a slash
  (`team/backup`) was described, and remedied, as a direct URL; the configured
  remotes now decide.
- **H11.9 done** (2026-11-06, merged at `d8375d1`; Codex diff review and recheck).
  Named locks live in `<runtime>/locks` (owner-only on POSIX). While
  `oslock.LEGACY_LOCKS` is on, each also takes its old file in
  `<tempdir>/smriti-locks`, always after the private one, so pre-H11.9 processes
  and new ones exclude each other. A lock never takes files its rivals don't: a
  refused runtime directory falls back to the old file alone only while the old
  files are on (with them off it fails closed: review P2), and an old file that
  can't be opened fails closed, because an old holder's descriptor outlives a
  permission change (review P2, which removed a POSIX "private alone" shortcut).
  A later release ends the transition by setting
  `LEGACY_LOCKS = False` (`SMRITI_LEGACY_LOCKS=0` ends it for one install
  sooner), once no pre-H11.9 process can be running, and only where every process
  of the user resolves the same runtime directory: on POSIX one started without
  `XDG_RUNTIME_DIR` (cron) resolves another, which the old file still covers for
  now. Old/new process contention tests for `edit_lock` and the writer lock fail
  against a plain move.

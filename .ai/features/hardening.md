---
status: in progress
opened: 2026-11-05
goal: an outside, no-context reviewer finds no P1/P2 issue and would adopt smriti as a dependency (see .ai/knowledge/quality-bar.md)
baseline: outside reviews at 4c9c9dd
---

# Hardening

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

## H9. From outside review #3 (at `3074a41`)

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

## H11. From outside review #4 (at `f1319a1`)

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

### H11.6 design

- **The reference profile is its own distribution**, in this repo:
  `profiles/reference/`, `smriti-reference` 0.2.0 (package `smriti_reference`,
  `dependencies = ["smriti"]`), registering `reference = "smriti_reference:PROFILE"`
  in the `smriti.profiles` entry-point group. Moved with `git mv`; only import paths
  change, so tool names, descriptions, the persona and the wake sections are the same
  bytes. Its tests move with it, with a conftest that isolates the memory root as the
  core's does. Generic checks stay in core.
- **Profiles load through entry points.** `load_profile(name)` keeps the name check,
  then takes the one entry point called *name* in `smriti.profiles`. None, two or
  more (the error names the packages), any exception while loading (chained), or an
  object that is not a `Profile`: each is a `ProfileError`. The callers keep their
  outcomes: the MCP server serves the core's tools, the wake shows one line, the
  morning fails visibly. An entry point is code from the same Python environment,
  so the trust boundary is unchanged; nothing is loaded from the memory tree.
- **The core's tests don't need the reference package.** They install a synthetic
  profile, `sample` (Ari and Sam), through real dist-info metadata in a temporary
  site directory (`tests/_profiles.py`); the server-subprocess test gets it through
  PYTHONPATH. A core test checks that `src/smriti` has no `profiles/reference` and
  no import of `smriti_reference`; CI builds the core wheel and fails on any member
  named `reference`. Building it found the wheel already failing with current
  hatchling (the eval corpus added twice); fixed first.
- **A neutral default root.** `tree_root()` is `SMRITI_ROOT` (or `NARADA_ROOT`); else
  `~/.narada` if it is a directory, which wins even beside a `~/.smriti`, so an
  existing install never switches roots unasked; else `~/.smriti`.
  `tree_root_source()` names the rule; `smriti status` prints it, and warns when both
  directories exist and no variable chooses. Every hard-coded `~/.narada` in `src/`
  and `scripts/` asks `tree_root()` at call time; the deployed `backup.py` and
  `narada-p.sh` default to the tree they are deployed in. Legacy identifiers stay:
  the qmd collection `narada`, the day-log's old speaker words, the `narada/`
  template directory, `narada-p.sh`.
- **The reference install's switch-over**: install `profiles/reference` (editable,
  `--no-deps`) into the environment that runs smriti when its checkout moves to this
  code. Without it the profile is missing: core tools only, a one-line wake note,
  and a morning that refuses to send. Its root doesn't move (`~/.narada` exists, and
  the hooks set `SMRITI_ROOT`).

## H12. From outside review #5 (at `bcec9a2`, v0.2.0)

Every problem review #4 named is fixed. Opus's five are new, one layer deeper:
packaging, privacy and module boundaries. All five, and the torn day-log line,
were checked against the code and hold. Sonnet's first (broad excepts without the
convention's reason) holds as a gap between the written rule and ruff's, which
accepts a handler that logs or re-raises.

**Invariant** (Suti, 2026-11-06): hardening changes no key function and costs no
speed. Capture, the nightly, the morning message, the wake, search, writes,
backups and the MCP tools work for an existing install as they did, and each
branch is benchmarked against master (MCP startup, cold and warm reads, writes,
the wake, the CLI read, incremental indexing) before it merges; a slower number
is a finding. In order:

| # | Change | Acceptance |
|---|---|---|
| H12.0 | Speed back to at least the pre-hardening level where the benchmark (2026-11-06, `13ac56a` against `bcec9a2`, a copy of the reference tree) found it slower: a warm `smriti_read` (169 to 187 ms), a `smriti_write` (46 to 81 ms) and the wake (0.71 to 0.91 s). Causes, profiled: the embedding client opens a TCP connection and authenticates for every request (the connect alone 14-21 ms on Windows), so the client keeps one authenticated connection and reuses it while the service advertises that it can, with an idle close so the connection cap holds; and the profile lookup reads every installed package's metadata in each new process (150-200 ms in a 683-package environment), so the scan is cached in the runtime directory, keyed on the import path's directories. The benchmark becomes `scripts/bench.py` (queries from a file, no personal text). | `scripts/bench.py` against `13ac56a`: no measure slower; the cold first query, the CLI read and the server's memory keep their gains. |
| H12.1 | Credentials are masked on every path into the tree: the project tools (findings and their per-tag stubs, decisions, feature specs, todos), the handoff, and what the digest and rollups write. | A planted credential shape is masked on each path; one test per path. |
| H12.2 | The nightly's codex fallback is opt-in (`"nightly_fallback": "codex_cli"` in daylog.json, or `SMRITI_NIGHTLY_FALLBACK`), since its sandbox can read the user's files while it reads untrusted transcripts. The reference install turns it on. The morning follows the same setting: `d2e0518` dropped its codex fallback without saying so (functional audit, 2026-11-06), so a day `claude -p` fails sends nothing; with the fallback on, the morning falls back again, without web search. | Tests for both settings, nightly and morning; PRIVACY.md. |
| H12.3 | Keyword search keeps non-ASCII terms: each term is quoted for FTS5 instead of stripped to `[A-Za-z0-9_]`. | Tests with `café`, Devanagari (`स्मृति`) and CJK queries, keyword-only. |
| H12.4 | A wheel install runs its installer: the deployed scripts and the memory template move into the package and are read with `importlib.resources`. The projects root is recorded at install and reused, in place of a hard-coded `C:/Projects`, so an existing install keeps linking the same projects. | A CI job installs the built wheel in a fresh venv and installs into a temporary home. |
| H12.5 | One provider-selection rule, used by the indexer and the service (today the env override wins in one and the recorded provider in the other); names used across modules made public (no private imports between packages); `serverInfo.version` from `smriti.__version__`; the `smriti_add_todo` text matches its whole-phrase match; `search_with_status` builds its results once. | A test for the shared rule; a test that no module imports another package's private name; a version test. |
| H12.6 | Capture recovers from bad input instead of losing turns. The day-log writer repairs a torn last line before appending (a newline first when the file doesn't end in one). A transcript line longer than capture's 8 MiB read (a pasted image is one base64 line) is skipped and counted as a parse error, where today `collect` re-reads it forever and every later turn in that file is lost without an error (Codex review, verified at `collect.py:100` and `complete_lines`). The service's counters are locked. | A test with a truncated line: the next turn parses. A test with a 9 MiB line: the turns after it are captured and the error is counted. |
| H12.7 | Every `except Exception` that stays carries `# noqa: BLE001 — <reason>`, or is narrowed (the API providers to their SDK's error base). | A test that scans `src/` and fails on a broad except without the reason. |
| ~~H12.8~~ | Dropped (Suti, 2026-11-06): removing the legacy integrations would change what the reference install runs (its PreCompact capture hook in Claude Code and Codex). They stay until nothing uses them. | |
| H12.11 | The nightly holds a run lock, so a manual run and the scheduled one can't overlap, and its render, digest and rollup writers use unique temp names. A digest hashes the turns it summarised: render and digest work from one read of the day. (Codex, high effort; verified.) | An overlap test: the second run waits or reports; a test that appends a turn between render and digest and finds the digest stale. |
| H12.12 | Rollup freshness follows what it read: a member's file content (not only the turns behind it) and the rollup prompt's fingerprint; provenance is taken before the LLM call. (Codex, high effort; verified at `rollup.py:49`.) | A regenerated digest with the same turns makes its week stale; a prompt change does too. |
| H12.13 | A failed backup commit or push reaches the nightly's status and `smriti status` (the hourly run's outcome as well), not only `.git/smriti-backup.jsonl`; the script still never blocks the caller. (Codex, high effort.) | A test with an unreachable remote: the status says the push failed. |
| H12.9 | Quality with real models, not only stubs: a weekly CI job runs the `needs_model` tests with the model cached, and `smriti eval` grows a versioned set (paraphrases, irrelevant queries, contradictions, corrections) whose retrieval and abstention scores, and the digests' coverage, are reported. | The job, green; the scores recorded per release. |
| H12.14 | `index_tree` splits into scan, plan, embed and commit, and embeds in bounded batches, so a full index's memory stays flat; embedding gets one public API that the indexer, search and the service share (no private imports between them). | A slow-marked test indexes about 100k chunks under a memory ceiling; the benchmark's index times no slower. |
| H12.15 | SECURITY.md lists every outbound data flow (transcripts to the LLM CLI, the tree to the git remote, hashes to the timestamp calendars, the model download, the morning message) and states the private store's threat model plainly. | The doc. |
| H12.0b | The embedding model stays loaded while sessions are open, as it did when each server held its own: an MCP server sends the service a heartbeat while it runs, and the service exits 15 minutes after the last client is gone, not after 15 minutes without a request. Today, after a 15-minute pause, a session's next search is keyword-only and its next write waits for the model (functional audit). | A test: a client that only heartbeats keeps the service; a service with no clients exits; one 1 GB process however many sessions. |
| H12.0c | Two more costs over `13ac56a` (Codex regression audit of the whole hardening, 2026-11-06, confirmed with probes). A write's `index_file` fills other sources' pending vectors before it returns, and its 32-chunk cap still admits one whole source of any size: so a write waits, up to the service's 30 s deadline, for unrelated files when there is a backlog. The repair moves off the write (the service's own repair loop, and the nightly's index, already fill pending vectors), or runs strictly within the chunk budget when no service runs. And `day_tz` stats `daylog.json` once per captured turn to check its cache (0.59 s against 0.03 s per 10,000 turns): it checks at most once a second, or once per batch. | A write with a large pending backlog returns in its own time; a 10,000-turn reconcile's day lookups cost no more than at `13ac56a`. |
| H12.17 | Capture reads Codex CLI 0.163's session files: since 2026-10-22 Codex no longer writes the message records the adapter reads, so no Codex turn has been captured since (functional audit; before the hardening, but a key function). | A fixture from a current Codex rollout: its turns are captured. |
| H12.16 | Small items: an append to a daily file keeps the file's line endings (H1.4's binary append made new entries LF beside CRLF ones on Windows); `smriti eval --search` is accepted again as the default mode; MCP answers a malformed message with a parse error (-32700) instead of dropping it; Python 3.13 joins the CI matrix; branch coverage is reported in CI with a floor; stale comments fixed (`index_policy`'s private-README line); comments in touched modules state their invariant, with incident history in the commit message. | CI; a parse-error test. |
| H12.9 | A weekly CI job runs the `needs_model` tests with the model cached. | The job, green. |
| H12.10 | Outside review #6. | Grades recorded. |

What an A needs, in the reviewers' own words (2026-11-06, `09fd037`, asked
directly; Opus B, Codex C on that run, which is outside the trend):
- Both: the LLM fallback can't reach a shell, and every write into the tree is
  masked at one chokepoint, shown by adversarial tests with canaries (H12.1,
  H12.2); capture recovers from torn and oversized records, shown by fault
  injection, and a stuck source is visible (H12.6); a self-contained wheel,
  smoke-tested from a clean environment (H12.4).
- Codex: nightly results from immutable input snapshots, serialised runs and
  provenance bound to the bytes consumed (H12.11); complete, transitive
  invalidation (H12.12); retrieval and synthesis quality measured with real
  models (H12.9).
- Opus: clean module boundaries and bounded memory in indexing (H12.5,
  H12.14); the legacy integrations cut or split out (H12.8, dropped: they
  stay while the reference install uses them); outbound flows and the private
  store's threat model written down (H12.15); comments that state invariants
  and a coverage floor (H12.16); and a second install, run by someone other
  than the maintainer on Linux or macOS with native scheduling, green for
  weeks. That last one is evidence code can't supply.

Not doing:
- **Masking stays pattern-based** (Sonnet 2): it catches credential shapes and
  says so. A PII framework is out of scope.
- **The bus factor and the shape of one setup** (Sonnet 3 and 4) aren't code
  changes. H11.6 took the setup's own code out of the package.
- **Incident history in comments** (Sonnet 5, Opus on readability): a pass that
  keeps each comment's rule and moves its story to the commit message belongs
  with the modules H12 touches, not a sweep of its own.

### H12.0 design

- **Keep-alive** (`smriti.embed`). The service's hello advertises
  `"keepalive_s": 10`. After a reply it reads the next request on the same
  authenticated connection, one at a time, until the client closes it,
  nothing arrives within `keepalive_s`, or it stops or drains (a waiting
  connection looks every 0.1 s, so a drain never waits one out). The
  connection keeps its slot; the cap of 32, the handshake, the 4 KB pre-auth
  frames and every read's deadline are unchanged, and only a peer with the
  key gets past the hello. The client keeps one connection per
  `ServiceEmbedding`, keyed on the discovery file, instance and port, and
  taken out under a lock, so concurrent calls open their own. It reuses it
  while the hello advertised keep-alive and it has been idle for less than
  `keepalive_s` minus 1 s. A kept connection whose end is already readable is
  dropped unused. One that fails on send, or closes before the first reply
  byte, is retried once on a fresh connection, for `embed` and `status`
  only; `stop` always goes on its own and is sent once. An old client closes
  after one request, and its EOF frees the slot. A new client finds no field
  in an old service's hello and connects per request. The protocol number
  stays 1. While the kept connection goes to the instance in the discovery
  record, the client doesn't probe that pid: with psutil on Windows the
  probe read the process table, about 4 ms of every request. A service that
  has died since is found when the connection is used, and replaced as before.
- **Profile cache** (`smriti.profiles`). The scan reads only the existing
  `sys.path` entries, less `''`, the current directory and the running
  script's directory, with `importlib.metadata.distributions(path=...)`,
  keeping the first distribution of each normalised name as `entry_points()`
  does. Its result is cached in the runtime directory as
  `profiles-<crc32>-<n>.json`, one file per list of entries, holding the
  fingerprint (each entry and its `st_mtime_ns`) and the entries (name,
  value, group, dist, version), written atomically. It isn't written while an
  entry changed in the last 2 s, since a second change in the same timestamp
  tick wouldn't show. A missing, corrupt or stale cache means a scan and a
  rewrite. A name not found is still scanned for again before "none", which
  now also refreshes the cache. With a warm cache, `load_profile("reference")`
  takes about 8 ms in a fresh process: 4 for the lookup, 4 for the profile's
  own import.
- **Where it differs from the brief, and why:**
  - The running script's directory is left out too. The wake hook runs
    `<root>/.smriti/wake.py`, so that directory is first on its path; it
    changes with every index write (the cache would rarely match), and a
    profile must never come from the memory tree.
  - A cached entry is loaded by an equivalent of `EntryPoint.load`, not by
    rebuilding an `EntryPoint`: importing `importlib.metadata` costs about
    50 ms (it brings the email package), more than the 20 ms target.
  - Entries are told apart by `os.stat` (device and inode), and the
    fingerprint keeps each entry's absolute path: resolving each path cost
    3-7 ms for 19 entries. The file name is a CRC, since importing hashlib
    costs 8 ms and the file holds the full fingerprint anyway.
  - The runtime-directory logic moved to `smriti.core.rundir` and is
    re-exported from `smriti.embed.runtime`, so the lookup doesn't import the
    embedding protocol (socket, sqlite3, hmac).
  - The benchmark found two more measures slower than at `13ac56a`, and they
    are fixed here too. `smriti status` went from 300 to 650 ms: the read-only
    open loaded sqlite-vec, whose package imports numpy, and the store
    package's `__init__` imported the indexer. The MCP server's first
    `tools/list` took 41 ms, importing the day-log writer to read daylog.json.
    Status now opens without vectors, and `index_tree` and `append_turns` are
    resolved on first use. The embedding client imports `socket`,
    `subprocess`, `secrets`, `hmac` and `base64` where it uses them, so
    status, with no service running, reads the discovery file without them.
  - The wake imported every installer helper with `wake_runner`'s package,
    and since `736a6b2` the agent-doc helper brought the file locks too. The
    package resolves its re-exports on first use, and `managed_doc` imports
    `write_atomic` when it writes. The reference profile still imports
    `smriti.mcp.tool` to build its tools: `Profile.tools` is a tuple of them,
    and deferring that would change the contract.
  - Each request also re-resolved the memory root (0.6 ms on Windows) and
    ran `mkdir` on the runtime directory; the client now computes its
    service files once per runtime directory, and `runtime_dir()` creates
    the directory only when it is missing.
  - A retried call kept its last failure as an exception, which held the
    client in a reference cycle, and with it the kept connection, a service
    slot, until the collector ran. The flood test caught it once in eight
    runs. The reason is now kept as text.
- **`scripts/bench.py`** runs the comparison: two checkouts or refs, fresh
  copies of a tree, an unmeasured warm-up per side, alternating rounds, a
  table with B/A, and a top-5 parity check per query. Five benchmark runs
  went into H12.0; the last is below.

Result (`scripts/bench.py`, 2026-11-06; a copy of the reference tree, 10
queries, a warm-up and two alternating rounds each; A is `13ac56a`, B is a
clean export of `8a20716`):

| Measure | A: 13ac56a | B: 8a20716 | B/A |
|---|---|---|---|
| MCP server: start and initialize | 156 ms | 167 ms | 1.07 |
| tools/list, first | 0 ms | 28 ms | (once per server) |
| tools/list, warm | 0 ms | 2 ms | (per call) |
| smriti_read: first query | 14.85 s | 814 ms | 0.05 |
| smriti_read: until vector results | 14.85 s | 15.62 s | 1.05 |
| smriti_read: warm p50 | 165 ms | 166 ms | 1.01 |
| smriti_read: warm p90 | 180 ms | 176 ms | 0.98 |
| smriti_write: p50 | 57 ms | 61 ms | 1.08 |
| smriti_write: max | 73 ms | 83 ms | 1.14 |
| wake hook (median of 3) | 697 ms | 720 ms | 1.03 |
| `smriti status` | 212 ms | 295 ms | 1.39 |
| CLI `smriti read` (median of 3) | 25.24 s | 11.79 s | 0.47 |
| `smriti index`, nothing changed | 16.51 s | 15.70 s | 0.95 |
| `smriti index`, 5 new files | 16.83 s | 15.79 s | 0.94 |
| MCP server memory | 1041 MB | 45 MB | 0.04 |
| embedding service memory | - | 1038 MB | - |

Top 5 the same for all 10 queries; the wake briefings identical (9,446
characters); 13 tools on both sides. Before H12.0 (`bcec9a2`): warm read
187 ms, write 81 ms, wake 0.91 s.

What is still over the baseline, and why it stays:

- **`smriti status`, +83 ms: new information shown.** It now reports the
  embedding service, pending vectors and capture. With a service running, as
  in every bench round, it connects and authenticates to ask the service for
  its live state (served, queued, idle): about 315 ms against 270 with none
  running. With none, what's left is the `smriti.embed` modules behind the
  line, about 20 ms (medians of 8 alternating runs: 269 against 249).
- **`tools/list`, +28 ms once per server and +1.5 ms per call: the profile
  seam.** The first call reads daylog.json and loads the profile: the cached
  lookup (4 ms) and the profile's own import. Each call re-reads the config,
  so a change of profile takes effect without a restart.
- **`smriti_write`, +4 ms p50 (1.08) and +10 ms max: safety features.** The
  embedding costs the same through the service as in-process (15.8 against
  16.5 ms for 26 texts). The write path's own work takes 3-7 ms more: H1's
  write transaction, with its identity check and source bookkeeping, the
  edit lock, and the `index.md` upkeep. The max is each session's first
  write, on both sides (A 83 and 56, B 91 and 76). Rounds of the same code
  vary by 10-20 ms here.
- **Within noise:** MCP initialize (+11 ms; run 4 had 162 against 162), the
  wake (+23 ms, 3%; 747 against 734 over 8 alternating runs), and the time
  until vector results (+5%). In B the model loads in its own process,
  started by the first query, which a keyword answer meets at once; the bench
  polls every 0.5 s.

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
- **H8 done** (2026-11-05). Review #3 at `3074a41`. The findings are H9.
- **H9.1-H9.11 done** (2026-11-05). Codex reviewed each branch; real findings
  fixed with regression tests: a missing crypto extra left an empty key that
  blocked the store; the day zone ignored an explicit root; the installer
  missed a running logd (logd now follows daylog.json), trusted a git-crypt
  attribute without its filter, trusted ignore rules over tracked and waiting
  files, sampled too few log outputs and only one push URL. CI now runs ruff
  with E501, B and BLE, and `mypy --strict` on the whole package, on every OS;
  getting there found numpy's and the Anthropic SDK's stubs differing by
  version from the development machine's.
- **H9.12 done**: outside review #4 at `f1319a1`; its findings are H11.
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
- **H11**: done. H11.8: outside review #5 at `bcec9a2`; its
  findings are H12.
- **H12**: planned, awaiting approval.
- **H12.0 implemented** (2026-11-06, `hardening/h12a`; not yet reviewed or
  merged): keep-alive, the profile cache, `scripts/bench.py`, and the
  fixes the benchmark found. The three slower measures are back at the
  baseline (warm read 166 ms against 165, write 61 against 57, wake 720
  against 697); the result is under "H12.0 design".
- **H12.0b implemented** (2026-11-06, `hardening/h12b`; not yet reviewed or
  merged). The service gains `hold`: authenticated and workless (it resets the
  idle timer and answers with the instance, state and `idle_s`), advertised in
  the hello as `"hold": true`; the protocol stays 1. A running MCP server marks
  itself at its first tool call (`smriti.embed.hold_while_running()`), and from
  its client's first successful embedding a daemon thread holds at once, then
  every `idle_s / 3`: the service exits `idle_s` after the last request or
  hold. A gone or draining service is started again by the next hold (the
  usual throttled spawn); an older service's "unknown op" stops holds to that
  instance (one debug line) until the instance changes. `smriti read`, hooks
  and the nightly don't hold. Where it differs from the brief: each hold goes
  on a connection of its own, so it never takes the one kept for requests
  (about 20 ms of background work every 5 minutes); the first hold goes at
  once, which tells the client the service's `idle_s` and whether it can be
  held; a service that *failed* to start is left to the next search or write,
  so a broken model load isn't retried in the background every 5 minutes; the
  client acts on the "unknown op" answer, not on the hello's flag, which
  covers every pairing in one round trip per instance; and the server is
  marked at its first tool call, not at startup, so starting imports exactly
  what it did (a test checks it).
- **H12.0c implemented** (same branch). Through the service, a write no longer
  fills other sources' pending vectors (the service's loop and the nightly's
  index do); in-process, it fills only the sources that fit in what is left of
  32 chunks (`fill_pending_vectors(strict=True)`), and the rest wait for the
  nightly. `day_tz` trusts what it read of a root's daylog.json for a second
  (a monotonic clock), then one stat says whether the file changed; the root
  is worked out again only then, so a change of `SMRITI_ROOT` is also seen
  within the second; `SMRITI_DAY_UTC_OFFSET` is parsed once per value. 10,000
  `day_tz` lookups on this machine: 5.3-7.7 s at master (a root resolve and a
  stat each), 22-27 ms now, 26-67 ms at `13ac56a`; `local_day` is within noise
  of `13ac56a` for an offset or an IANA zone (the reference install's), while
  the system zone's per-instant OS conversion (H9) costs about 5 µs a turn
  more.
- **Benchmark** (`scripts/bench.py`, 2026-11-06; the reference tree's copy, 10
  queries, two alternating rounds; A `663525a`, B `f763543`): no measure
  slower. MCP initialize 162 against 174 ms (rounds 155/168 against 180/168;
  ten alternating starts: 217 against 188 ms, the same 125 imports); first
  query 671/658 ms; until vectors 14.95/14.71 s; warm read p50 171/166 ms, p90
  187/176; write p50 58/51 ms, max 84/74; wake 733/697 ms; `smriti status`
  276/281 ms; CLI read 11.68/11.30 s; `smriti index` 15.88/15.86 s unchanged,
  15.88/16.52 s with five new files (rounds 15.7-16.1 against 16.3-16.8; it
  runs no code these commits touch); memory 46/46 MB and 1038/1037 MB. Top 5
  the same for all queries; the wake briefings identical.
- **H12.1 implemented** (2026-11-06, `hardening/h12p`; not yet reviewed or
  merged). `smriti.core.memwrite` is the one way memory text reaches the
  tree: `write_memory` (an atomic replace), `create_memory` (an exclusive
  create, suffixed or not) and `append_memory` (under the edit lock) mask the
  whole text, then write through `smriti.core.files`. Through it: `smriti_write`,
  the project tools (todos, findings with their per-tag stubs and the
  cross-project index, decisions, feature specs, status notes), the handoff,
  the reference profile's outbox, digests and rollups, and two the brief
  didn't list, rendered days and `index.md` listings (a day captured before a
  pattern existed, a heading written by hand). A name that becomes a path is
  masked by its caller first (a finding's or decision's title, a tag), and a
  feature slug shaped like a credential is refused, since it names the spec
  for life. A read-modify-write masks the whole file it writes; no project
  mirror on the reference install holds text the scrub would change.
  `tests/test_memwrite.py` scans `src/` and the profile (AST) for file writes
  and fails on any outside the chokepoint and a 33-entry allowlist of
  runtime and index state, each with its reason, or on a stale entry;
  `tests/test_masked_paths.py` and the profile's tests plant credential
  shapes in each path, a fake model answering with them for the digest and
  the rollup, and every one fails with the mask removed. Where it differs
  from the brief: the scrub cost 0.4 ms per kB (19 ms for 50 kB of ordinary
  text, 35 ms for the reference tree's largest `index.md`), too much for
  index pages and renders, so each pattern got a hint (a literal it needs)
  and the `\b`-led ones a literal-first form, with the same masks
  (`tests/test_scrub.py` holds it to the old patterns over 4,000 fuzzed
  texts): 50 kB 0.6 ms, that `index.md` 1.8 ms, a typical day's render (0.3
  MB) 20 ms more on its 20-40 ms, the largest day (13.6 MB) 1.3 s more on
  0.5 s; and the chokepoint lives in `smriti.core`, not `store`, so a tool's
  first call imports nothing new but the scrub.
- **H12.2 implemented** (same branch). No fallback unless one is asked for:
  `"nightly_fallback": "codex_cli"` in daylog.json, or `SMRITI_NIGHTLY_FALLBACK`,
  which wins when set (empty: none); `compose` takes the config, and the
  digest and rollups pass theirs. The morning follows the setting:
  `compose_message` composes on claude_cli as before and, when that fails with
  the fallback on, on the fallback with the digest-only prompt (a profile's
  persona kept, no headlines) and the codex seat's own call, whatever
  `morning_web_search` says; `smriti morning --dry-run` uses it too. The
  codex provider's docstring now says what read-only allows. **The reference
  install must set `"nightly_fallback": "codex_cli"` in its daylog.json when
  this merges** (its test config does).
- **H12.16, the line endings, implemented** (same branch). An append takes
  the ending of the file's first line, and an entry's own CRLFs follow it; a
  new file gets the platform's, as `13ac56a`'s text-mode `os.open` gave. The
  outbox appends share it (they were text mode: CRLF on Windows, whatever the
  file had). The tests write the file's ending first, so both run on every
  platform.
- **Benchmark** (h12p; `scripts/bench.py`, 2026-11-06, the reference tree's
  copy, 10 queries, two alternating rounds; A master `fc27d39`, B `4ab4c32`,
  the last code commit): no measure slower beyond noise. MCP initialize
  189/176 ms; tools/list first 38/27 ms; first query 888/974 ms (rounds
  1137/639 against 984/964, inside A's spread; a read imports the same 212
  modules on both sides); until vectors 17.07/17.12 s; warm read p50 188/182
  ms, p90 210/196; write p50 75/60 ms, max 105/107 (rounds 99/112 against
  121/94; a write imports one module more, `smriti.core.memwrite`); wake
  728/772 ms, slower in both rounds, but it imports the same 149 modules and
  runs no code these commits change, and eight alternating runs afterwards
  gave 716 against 723 ms (spreads 692-786 and 702-897); `smriti status`
  298/307 ms; CLI read 12.56/12.69 s; `smriti index` 16.05/16.22 s unchanged
  and 16.28/16.50 s with five new files (rounds 16.06/16.04 against
  16.62/15.82, and 16.60/15.95 against 17.14/15.86); memory 46/46 MB and
  1039/1037 MB. Top 5 the same for all queries; the wake briefings identical
  (9,478 characters). The nightly's new costs, measured apart: a typical
  day's render 20 ms more, the largest day's 1.3 s more; a digest's or a
  rollup's mask under a millisecond.
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
- **H11.6 done** (2026-11-06, merged at `a6c24c0`; Codex diff review, no findings).
  The reference profile is `smriti-reference` in
  `profiles/reference/`, loaded by entry point; smriti's wheel has none of it (a CI
  step builds the wheel and checks), and the core's suite passes without it
  installed. The default root is `~/.smriti`, with an existing `~/.narada` kept;
  `smriti status` names the rule. Merge review: reading the entry points cost
  145 ms a call in a 683-package environment, on every MCP request; they are now
  read once per process, and again before a profile is reported missing. The
  reference install switched over with its tools, wake sections and morning prompt
  unchanged (compared before and after; three core descriptions now say `<root>`).
- **H11.7 done** (2026-11-06). `v0.2.0` tagged at `ddf907b` and published as a
  GitHub pre-release; the changelog gained the H11 changes it lacked; README and
  PRIVACY.md disclose the model download (`gpahal/bge-m3-onnx-int8`, about 560 MB),
  `HF_HUB_OFFLINE`, and the other network requests; PRIVACY.md's phases no longer
  name the releases that would carry them.

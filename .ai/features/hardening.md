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

## H13. From outside review #6 (at `5323e58`)

Both reviewers ran at high effort on a tree with no history. Every claim below
was checked against the code. The H12 invariant holds: no key function
changes and nothing gets slower, unless Suti decides otherwise for an item
marked *(decision)*.

| # | Change | Acceptance |
|---|---|---|
| H13.1 | An index follows the root it is given: `index_tree(root=...)` and `index_file_result(root=...)` take their database (and embedding context) from that root, not from the environment, so a root B can't write into, or purge, root A's index. The installer's MCP registration carries the memory root. (Codex 1) | A test with two roots: each index holds only its own sources. |
| H13.2 | Keyword search reads its candidates and their content in one snapshot (the vector leg's `_snapshot`, or one joined query), so a write between the two can't pair a hit with other text. (Codex 4) | Codex's reproduction as a test: a concurrent rewrite never returns another chunk's content. |
| H13.3 | A failed `git status` is a failed backup: `has_changes` doesn't read it as "nothing to commit", and the status reports success only with the events that show it. (Codex 3) | A test with an unreadable index: the run is not ok and says why. |
| H13.4 | The scrub masks quoted keys (`"password": "..."`, `"token":"..."`, YAML and JSON forms), with adversarial tests, keeping the literal-first speed. (Codex 2) | Masked; the fuzz equivalence test updated deliberately; the scrub bench no slower. |
| H13.5 | The installer writes `~/.claude.json` and `~/.claude/settings.json` atomically, under a lock, keeping the first backup (timestamped, never overwritten); HOME is read when called. (Opus 4) | Tests; a second install keeps the original backup. |
| H13.6 | `core` imports nothing from `store`, `daylog`, `embed` or `mcp`: the scrub and the day zone move to `core` (re-exported where they were), and `oslock` takes the runtime dir from `core.rundir`. A test fails on an import against that direction. (Opus 3) | The direction test. |
| H13.7 | Durability: `write_atomic` fsyncs the temp file before the rename. *(decision: an fsync per write costs time; it is measured first and only kept if within noise, or with Suti's say)* (Opus 5) | The bench's write measure. |
| H13.8 | The nightly's work stops growing with history: the stale-day scan is incremental (a marker of what was checked), the indexer gets its setting without changing the process environment, and `index_tree`'s purge joins the transaction its docstring promises. `read_day_turns` reports what it skipped. (Opus 5) | The nightly bench no slower; a test for the purge's transaction. |
| H13.9 | The private store's legacy key: found in the old README location, it is moved to `.key` and the user is told the old key is in git history and should be rotated. (Opus 1) | A test of the migration and the warning. |
| H13.10 | Comments state current design: the remaining history ("the connection-closed era", review numbers, H-item IDs in code) goes to commit messages; redundant local imports go (lazy imports that keep startup fast stay, with the reason). (Opus 2) | A scan that fails on review numbers and H-IDs in `src/`. |
| H13.11 | *(decision)* The wake marks the agent-written memory it injects (handoff, journal, project blocks) as data, not instructions, against persistent prompt injection. It changes the text the agent wakes to. (Opus 1) | The wake's sections framed; nothing removed. |
| H13.12 | *(decision)* Search quality from the eval set: a later note that corrects an earlier one ranks above it (1 of 4 corrections resolved today), and abstention uses raw similarity, which separates answerable from irrelevant queries (AUROC 0.99) where the rescaled score doesn't (no irrelevant query abstains). It changes search results. | The eval set's corrections and abstention scores up; the other cases no worse. |
| H13.13 | Outside review #7. | Grades recorded. |

Not doing: the codex fallback stays opt-in and documented (both reviewers
call it a design risk, not a default); team and multi-user use is out of
scope; the bus factor isn't a code change.

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
- **H12**: done (merged by 5323e58; H12.8 dropped). H12.10: outside review #6 at `5323e58`; its findings are H13.
- **H13**: planned, awaiting approval.
- **H13.3 implemented** (2026-11-07, `hardening/h13b`; not yet reviewed or
  merged). The backup script logs a failed `git status` (`status_failed`, with
  git's code and the tail of its output) and still exits 0; it also logs
  `nothing_to_commit` and `up_to_date`, so the wrapper's summary claims a step
  only from the line that shows it: a finished run with no line for a step
  reports it `unknown`, not ok (a script deployed before this one, until the
  installer updates it). `backup_status` counts a failed status as a failed
  commit, and a later `nothing_to_commit` ends it. A corrupt `.git/index`: the
  run says `commit failed: git status failed: ...`, `smriti status` says
  `commit FAILED`, and the night fails. `privacy.backup_remote` copies only the
  push target's rule, and its git failures already raise into
  `GitState.error`: unchanged. A quiet hourly run now logs two lines (about
  200 bytes), within the log's cap. The reference install's deployed script
  needs the installer run (or a copy) to get this.
- **H13.4 implemented** (same branch). A rule for quoted keys runs before the
  bare names' rule: a key in either quote, escaped too (JSON inside a JSON
  string, as `curl -d` writes it), that is a secret's name or ends in one after
  `_`, `-`, `.` or a camel-case hump (`access_token`, `client-secret`,
  `db.password`, `accessToken`), then `=` or `:`, then the value, masked whole
  to its closing quote (escapes and spaces included), or, unquoted or
  unterminated, as far as the bare form's characters go. Decided: a key that
  only begins with a name (`password_hint`, `token_type`) isn't one; a value
  under 8 characters is kept, as for a bare name; names match ASCII case only
  (the odd Unicode folds, `ſecret`, don't make a key name); a secret under a
  non-secret key (`{"password": {"value": ...}}`) and a bare name's quoted
  value with other characters (`password = "p@ss word"`) are still not
  masked. The fuzz now holds the scrub to its patterns as first written, the
  new rule included, and the old patterns alone wherever the new rule finds
  nothing (of the 4,100 texts, 2,349 are masked and 969 differ from the old
  patterns, each where a quoted key is). Speed, master/branch, medians of
  three alternating runs (each a median of 15): two 50 kB days 0.919/0.924
  and 0.886/0.882 ms, a 2 kB note 38.7/38.1 us, a 1.6 kB note 32.8/33.1 us,
  1.5 kB of JSON with no secret key 17.0/16.8 us: the same. How:
  the two assignment rules share one scan for their hints; it looks for `key`
  and back for `api`, not for `api` (42 against 16 us per 50 kB: the engine
  scans for a literal's first letter, and an `a` is everywhere); the new
  pattern starts with a bare quote, which the engine skips to (in a group or
  after an optional backslash it was tried at every position, 1.1 ms per 50
  kB), takes the key without backtracking and looks back at its end. Text that
  does hold a quoted secret pays for masking it: 50 kB of a day plus one JSON
  secret 2.20 ms against 1.38, 1.5 kB of JSON with secrets 116 against 33 us.
  Codex review (P2, fixed in `84ac740`): in JSON inside a JSON string, the
  value's first `\"` was taken for its end, so a password holding a quote
  kept its tail, and a key escaped twice wasn't recognised. A value's
  escaping now has a level (its run of backslashes before the quote) and it
  ends only at that run and the quote; inside it, runs are read as escaped
  backslashes, then an escaped quote, or, before another character, as the
  value's (an outer level's `é` too), possessively, the pairs first (a
  long run was otherwise rescanned at each pair: 200,000 backslashes took
  seconds). Tests: seven values at four levels of escaping, each decoding
  back to the same object with only the secret replaced. Ordinary text the
  same (0.959/0.964 and 0.919/0.894 ms for the 50 kB days, 40.4/39.0 us for
  the 2 kB note); the fuzz now has 946 texts differing from the old patterns.
- **H13.5 implemented** (same branch). `integrations.common.config_file.edit_config`
  makes every edit of `~/.claude.json`, `~/.claude/settings.json` and Codex's
  `config.toml`: under the file's edit lock from the read to the write;
  re-reading it just before writing, and if the harness changed it, redoing
  the change on its text once, else refusing (the step prints `NOT SAVED`,
  `run_claude_code`/`run_codex` return False and the install exits nonzero);
  backing the file up first, as read, to a new `<file>.smriti-<UTC time>.bak`
  (exclusive create; one cut short is removed, or it would pass for the
  first), the oldest never removed and five kept in all; writing
  through `write_atomic` with the file's permissions (its new `mode`, set on
  the temp file before any content) and through a symbolic link to its
  target. Both installers work their paths out from the home when a step runs
  and take a `home`. Beyond the brief: the permissions and the link (an atomic
  replace would otherwise have made a 0600 `~/.claude.json` world-readable and
  replaced a dotfiles link with a file).
- **H13.7 measured, not committed** (2026-11-07): the fsync costs more than
  noise. The candidate fsynced the temp file before `os.replace` (and the
  directory after it, on POSIX). `write_atomic` itself, 400 alternating calls
  per size on this machine, without/with: 1 kB 1.01/1.79 ms median (p99
  1.4/50 ms), 10 kB 1.02/1.71 (p99 1.4/4.2), 50 kB 1.10/1.85 (p99 1.5/5.2):
  about 0.75 ms more a call, 1.75 times, with stalls. `scripts/bench.py` (A
  the branch without it, B with it; the reference tree's copy, 13 queries,
  two alternating rounds): `smriti_write` p50 46 against 61 ms (rounds 42.9
  and 49.4 against 44.2 and 77.5) and max 73 against 138 ms, B's second round
  holding two writes of 190 and 201 ms; nothing else beyond noise. A
  `smriti_write` makes one or two `write_atomic` calls (its `index.md`
  upkeep). The patch is kept out of the tree. Alternatives, for Suti: (a)
  fsync only where a lost file can't be regenerated, behind a
  `write_atomic(..., durable=True)`: the project tools' todos, feature specs,
  decisions and status notes, the handoff, the morning's ledger, the
  nightly's status, a harness's config; not `index.md` listings, renders,
  digests and rollups (rebuilt from their inputs by the next run), nor the
  runtime caches; (b) an environment flag (`SMRITI_FSYNC=1`) for installs
  that want durability over speed; (c) the nightly fsyncs what it wrote once,
  at its end, before the witness commit (the commit is the durable copy).
- **H13.6 implemented** (same branch). The scrub is `smriti.core.scrub` (moved,
  history kept) and the day zone `smriti.core.dayzone`; `smriti.daylog.scrub`
  and `smriti.daylog.model` re-export them. `oslock` takes the runtime
  directory from `core.rundir`, the store's writer the day zone from core.
  `tests/test_core_layer.py` scans `core` (AST: module level and functions,
  relative imports, `from smriti import x`, `import_module` with a literal) and
  fails on any smriti package but core; on master it names the two imports.
  Startup: the wake, `smriti status` and tools/list import what they did plus
  `core.dayzone`, the code that left `daylog.model` (importing `daylog.model`
  159.2 against 159.2 ms, the briefing 165.5 against 169.1 ms, medians of 30
  alternating processes).
- **H13.9 implemented** (same branch). Found in the README's code block with no
  `.key`, the key is written to `.key` as `init` writes one (exclusive,
  0o600), the same key so nothing becomes unreadable; its line, and its block
  if it held nothing else, leave the README (its line endings kept). A `.key`
  with another key still wins and the README is left, with a warning. The
  user is told that the key is in git history and to rotate it: a log warning,
  and, since the first command after an upgrade is likely the hidden hourly
  backup, `private/KEY-WAS-COMMITTED.txt`, which `smriti status` shows until it
  is deleted. The store can't rotate, so PRIVACY.md has the manual steps (a
  re-runnable `MultiFernet` script that keeps the new key outside the tree
  until it is done) and what rotation can't undo, the old ciphertext in the
  history. The README template no longer says the key is in it. Codex review
  (two P2s, fixed in `971fb08` and `1a4f625`): two commands starting together
  could both find the key's line, and the second then deleted whatever line
  had taken its index in the shorter README (or raised IndexError); the
  migration now holds the README's edit lock from finding the key to the
  rewrite, finds it again under the lock, and removes the line it finds in
  the text it rewrites. A write or read without `init` moved the key with no
  `private/.gitignore`; the ignore rule now goes in before any `.key` is
  written. Deviation: "if `.key` exists by then, leave the README alone"
  holds for a `.key` with another key; one with the same key is a migration
  cut short, and the README is cleaned, or the key would stay in every
  later commit.
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
- **H12.6 implemented** (2026-11-06, `hardening/h12c`; not yet reviewed or
  merged). The writer closes a torn last line before appending (the fragment
  becomes a line of its own), and the state records each day-file's lines that
  aren't records and the repairs; `smriti status` shows them. Collect's
  docstring states the persistence order: append, then fsync the day-files the
  pass wrote, then save the state by atomic replace, so the saved offsets never
  lead the log on disk (before, nothing ordered them, and a crash of the machine
  could lose turns the state had passed). Fault injection runs real capture in
  a child killed with `os._exit` mid-append, before the state save and during
  it: every turn readable after the next pass, none twice, none glued. A record
  longer than one 8 MiB read is skipped once its newline is written, 1 MiB at a
  time (peak memory one read, tested at 40 MiB), counted as a parse error and
  recorded (offset, bytes, time); until then it is waited for, and later passes
  read only what came since; a file that shrinks meanwhile is read again past
  its offset (Codex review, P2). A file that grew in two passes while its offset
  stayed put is stuck: logged once, and named in `smriti status` with the files
  waiting on a long record and the records skipped. On the reference install
  one Codex rollout has been stuck this way since 2026-10-27. Beyond the brief:
  a pass reads each day-file once (it re-read the whole file for every source
  file with new turns), which pays for the fsync; the same turn twice in one
  batch is written once; a malformed state entry is read again from the start
  instead of failing the load. Not done here: the row's "service's counters are
  locked" (embedding service), which this branch's brief didn't include.
- **H12.17 implemented** (same branch). The functional audit's date was late.
  Every Codex CLI 0.145 session on the reference install was captured; from
  0.153.2 (installed 2026-09-04 there) no CLI session gave a turn, and the
  rollouts 0.154 wrote, untouched since, are in the "paginated" history mode:
  turns are `event_msg`/`item_completed` records carrying a `UserMessage` or an
  `AgentMessage` item, with no `user_message` or `agent_message` event. The
  last real Codex turn in the reference log is from 2026-09-10. 0.163
  (2026-10-22) then rewrote older rollouts into the paginated mode, compressed
  as `.jsonl.zst`, which the glob doesn't match. The adapter reads both
  encodings with unchanged ids (the 118 rewritten rollouts of 28 sessions the
  reference install had captured, read as capture reads them, give back every
  one of their 5,945 turns outside the llm-workdir with its id; the legacy
  fixture's ids are pinned to `fc27d39`'s), keeps a message written in both
  once however the file is split between reads (Codex review, P2: a memory of
  the last five seconds' messages, as role, text hash, time and encoding, is
  kept in the file's capture state with its offset), and gives no turns for a
  session whose cwd is smriti's
  llm-workdir: the collector's path exclusion never covered Codex, so the
  nightly's codex fallback would have fed its digests back. Backfill: `smriti
  logd --rescan SOURCE` (procedure below). A read-only dry run on the reference
  install: 316 new turns over 2026-10-15..11-06, 14,490 already held; in the
  `.zst` archives (2026-09-04..10-15), 690 more in files under 15 MB compressed,
  and ten larger archives unread. Found on the way, not fixed: Codex Desktop's
  import of Claude Code sessions put 15,119 turns into 2026-10-22's log, all
  stamped with the import's minute (629 of them smriti's own llm-workdir
  sessions; 13% of the rest match a captured Claude turn's text). Writers never
  rewrite a day-file, so removing them is the maintainer's call.
- **Capture benchmark** (`scripts/bench_capture.py`, 2026-11-06; 20,000 Claude
  and 20,000 legacy Codex lines, six alternating rounds, medians; master
  `fc27d39`, this branch `fdf53db`, pre-hardening `13ac56a`): the pass that
  finds everything new 3641 / 2941 / 3735 ms; an idle pass 18.5 / 19.0 / 18.5
  ms (ten rounds at 2,000 lines: 18.9 against 18.5); a pass after 2% more lines
  1302 / 206 / 1341 ms; the same turns appended by each. The paginated encoding
  on this branch: 2899 ms, the same turns. No measure slower than master or
  `13ac56a` beyond noise.
- **Backfilling Codex turns** (H12.17). After this branch is deployed and logd
  restarted (a running daemon keeps the old code; `smriti logd --ensure` won't
  restart it): `smriti logd --rescan codex`. It sets the codex source's offsets
  in `log/.state.json` to 0 under the writer lock and runs one pass; files over
  8 MiB finish over the daemon's next passes. It appends only turns the day-log
  doesn't hold, to the days they belong to, and marks them dirty, so the next
  nights re-render and re-digest those days (ten digests a night, newest first)
  and rebuild their weeks and months. Safe to repeat. To see its effect first,
  run it with `SMRITI_ROOT` on a copy of `log/` and `.smriti/daylog.json`. For
  the archived sessions (2026-09-04..10-15): decompress copies of the
  `.jsonl.zst` files, keeping their names and `YYYY/MM/DD` layout, into a
  scratch directory (`zstd -d`; never in `~/.codex`), add it as `"extra_sources":
  [{"name": "codex-archive", "kind": "codex_rollout", "glob":
  "<dir>/*/*/*/rollout-*.jsonl"}]`, run `smriti logd --once` until a pass adds
  nothing, then remove the source and the copies. Ids match the rewritten
  records, so what the log holds is dropped.
- **H12.11 implemented** (2026-11-06, `hardening/h12n`; not yet reviewed or
  merged). A run holds a named OS lock (`nightly`, per memory root) from its
  reconcile to its status file. A second run waits 10 s, then returns a status
  saying it skipped (ok, not failed), writes no status file and exits 0; when no
  lock file can be opened at all, the night runs as before and says `"run_lock":
  "unavailable"` (oslock gains `try_lock`: held, busy or unavailable). Render,
  digest and rollup already wrote through `write_atomic` (H12.1's memwrite), whose
  temp name is the process's and thread's; the status file (named by the second)
  and the witness proof (a fixed `.tmp`) now do too, and `write_atomic` takes
  bytes. A digested day is rendered and digested from one read of its turns
  (`render_snapshot`): the digest gets the masked rendered text, those turns and
  their hash, and records that hash, so a turn appended after the read leaves
  both stale. Before, the digest hashed the turns as it began, and a later render
  alone (a day past the digest cap) made the pair look current: the turn never
  reached a digest. The model gets the same text as before (a test holds the
  snapshot's equal to DD.md read back). Tests: a barrier test of overlapping runs
  (one digests, the other skips and leaves the first's status), a run that waits
  for one finishing, eight concurrent writers of each artifact, and a turn
  appended between render and digest, with and without a render-only night
  after; each fails on `388cb24`'s code. Of the brief's two options for the
  snapshot, the first: the other would hold the writer lock across a render and
  a digest's LLM calls, holding up capture for minutes.
- **H12.12 implemented** (same branch). A rollup records `members_hash`, a
  manifest of its members' content (line endings aside) from the one read its
  body is built from, before its call: a digest regenerated from the same turns
  makes its week stale, a regenerated week its month. Digests and rollups record
  `prompt_fp` (their prompts, a digest's three; the budgets that decide what the
  call sees; the provider chain; both names), and a different one makes them
  stale. The upgrade regenerates nothing: an artifact without the new fields, as
  every existing one is, is judged by its old rule (the input hash; a rollup's
  `inputs_hash`) and gets them when it is next regenerated for a reason that rule
  sees. New rollups write `inputs_hash` by the old rule too, so an older smriti
  reads them as current. Heads are read from their first 8 KiB, which gives the
  2,000 characters reading the whole file did. One existing test changed: the
  scan test built a rollup without the config, so its prompt named the default
  agent; it passes the config now, as the nightly does. Scheduling check
  (`scripts/bench_nightly.py --schedule`, a copy of the reference tree, a stub
  model, capture off, five nights): `388cb24`, master `1af2076` and this branch
  schedule the same each night. Plain: 11-06's digest (three calls), then
  nothing. With a late turn appended to 10-15: that digest and
  `2026/10/week3.md` (the week by its old rule) the first night, `2026/10.md`
  the second, `2026/11/week1.md` when it closes. Nothing because of the upgrade.
- **H12.13 implemented** (same branch). `smriti.backup.backup_status` reads the
  last commit and push from `.git/smriti-backup.jsonl`, newest line first (a full
  log in 1-2 ms), and since when pushes fail. `smriti status` prints it in a line
  ("push FAILED since ..., last tried ...: <git's reason>"); the nightly records
  it as a `backup` step, and a failed last commit or push fails the night, so the
  wake names it. Where it differs from the brief, which asked for the outcome to
  be reported: failing the night is what makes it seen, and a push with nowhere
  to go (no upstream and no `origin`, or a detached HEAD) is reported as skipped,
  not failed, since the hourly task pushes on every install and a remote-less
  one's nights would otherwise fail daily. `trigger` returns what its own run did,
  from its lines: the deployed script now puts the caller's `SMRITI_BACKUP_RUN`
  on each line and its tag on the push line (an older script's lines are matched
  by position and tag; **the reference install's `.smriti/backup.py` gets the run
  ids when the installer redeploys it**). The witness step records the push it
  triggered; `smriti backup` prints the outcome and still exits 0; the script
  still never blocks or fails its caller. Tests use real git: an unreachable
  remote (status says FAILED and since when, the same "since" an hour later), a
  working one, none at all, and the witness's push. Codex diff review: three P2s,
  all in `backup.py`, fixed with regression tests that fail without the fix. A
  remote defined outside `.git/config` (an included file, the global or the
  worktree config) read as "nowhere to push", and the night as ok: git decides
  now (`privacy.push_has_destination`, a remote `git remote` lists or a URL or
  path, the rule `_is_direct` uses; not `git remote get-url`, which knows only
  the repository's own remotes while `git push` uses a global one too), asked
  only when the last push failed (one `git remote`, about 40 ms on that path; a
  healthy install's read is unchanged). A script that exited non-zero before
  logging read as "nothing to commit, up to date": the run is now not ok and says
  how it exited, keeping only what it logged. And a commit older than a
  failure, success, failure run of pushes stretched the streak back past the
  success. Found on the way, not changed: the privacy check's push URLs come from
  `git remote get-url`, so the installer names no destination for a remote
  defined only in the global config, though the backup pushes to it.
- **Nightly benchmark** (`scripts/bench_nightly.py`, 2026-11-06; fresh copies of
  the reference tree's `log/`, daylog.json and voice transcripts, read once
  before timing; alternating rounds, medians). Master `1af2076` against this
  branch, eight rounds: reconcile 443 / 456 ms (the same code); the stale-day
  scan 766 / 622; the render of the three latest days and the largest 1869 /
  1868; the rollup scan 27 / 26; the backup status read - / 2 (a full synthetic
  log); a whole nightly with both caps at 0 and no index or witness 1058 / 919
  (the branch's with the backup read). Against `388cb24`, five rounds: 520 /
  436, 757 / 611, 1830 / 1879 (1.03, inside both spreads; 1.00 over the eight
  against master), 27 / 26, 1012 / 925. No measure slower beyond noise. `smriti
  status` on a copy of the reference index: 259 against 267 ms over eleven
  alternating runs each (spreads 240-292 and 245-273); its new backup line costs
  about 5 ms, importing `smriti.backup` (three dataclasses) and reading a full
  log, the cost of the new information.
- **H12.7 implemented** (2026-11-07, `hardening/h12r`; not yet reviewed or
  merged). `tests/test_broad_excepts.py` scans the source (AST: `src`, the
  reference profile, `narada/` and `scripts/`) and fails on a handler catching
  `Exception`, `BaseException` or everything without `# noqa: BLE001 — <reason>`
  on its clause. `store/indexer.py`, `store/search.py` and `embed/` are left out
  until `hardening/h12s`, which restructures them, merges: **then drop
  `NOT_YET` from the test.** Of the eleven handlers without a reason, two were
  narrowed: the API providers to their SDK's error base (transport errors come
  wrapped as `APIConnectionError`) plus `ValueError` (a body that isn't JSON;
  anthropic's refusal of a non-streaming call too long for its timeout), so a bug
  in a call is no longer an `LLMError`, while the nightly's compose still records
  any provider failure. Nine kept with their reason: the MCP tool call and stdio
  loop, the private store's write, read and key cleanup (audited or cleaned up,
  then re-raised), the morning's compose (frees the day, re-raised), a profile's
  load, `ensure_schema`'s rollback, the wake shim. Of the 53 that had a reason,
  one more was narrowed (`witness._block`: a request, its reply and its JSON);
  the rest are top-level handlers, hooks, nightly steps or third-party calls
  (opentimestamps raises bare `Exception`). Where it differs from the brief: the
  rule now names the re-raise case (CLAUDE.md, conventions), and the scan covers
  `narada/` and `scripts/` too (one reason added, the wake shim's).
- **H12.15 implemented** (same branch). SECURITY.md's "What leaves the machine"
  lists each flow with what, to whom, when and the off switch, checked against
  the code; "The private store" states its threat model. Found on the way:
  `smriti read`'s reranker asks the Hugging Face Hub for its file list on every
  run (PRIVACY.md now says so), and `SMRITI_NIGHTLY_PROVIDER` can send the
  digests to an API provider. Beyond the brief: the agent's own session, a
  hosted embedding provider, and Ollama and qmd when pointed off the machine.
- **H12.16, the rest, implemented** (same branch). MCP: a line that isn't JSON
  gets -32700 and JSON that isn't an object -32600, each with a null id
  (`answer_line`); beyond the brief, a byte that isn't UTF-8 and JSON nested too
  deep, which both ended the server, now get -32700 (stdin read with
  `surrogateescape`). Startup imports the same 125 modules. `smriti eval
  --search` parses again and runs the same cases as `smriti eval`. Stale
  comments: `index_policy`'s private README, `private/__init__`'s committed key,
  `PrivateStore.init`'s README key, the outbox's consolidation pass. Not done
  here: Python 3.13 in CI, the coverage floor, and comments stating invariants
  in touched modules.
- **Two follow-ups** (same branch). The installer's privacy question named no
  destination for a remote defined only in the global or system config: `git
  remote get-url` doesn't know it, though `git push` uses it. Its push URLs now
  come from `git config --get-regexp` by git's own rule (push URLs rewritten by
  insteadOf, else URLs a pushInsteadOf rewrites, else URLs by insteadOf); eight
  shapes in a temp `GIT_CONFIG_GLOBAL` match `get-url` for the same remote
  defined in the repository, and a real push lands where the installer said.
  And `test_a_query_does_not_wait_while_the_service_is_starting`, which failed
  once on windows-latest: its writer polled the discovery file while the
  in-thread service replaced it, Windows refused the open mid-replace, and
  `read_discovery` read the PermissionError as no service, so a client that may
  not spawn raised "not running". The test's polls and the publish now take
  turns; 50 runs in a row pass. **The client has the same window** (a poll that
  meets a replace reads "no service": a spurious throttled spawn, or one "not
  running" without spawn): `read_discovery` could retry a PermissionError
  briefly, in `hardening/h12s`, which owns `smriti/embed`.
- **H12.3 implemented** (2026-11-07, `hardening/h12s`; not yet reviewed or
  merged). A query word with a letter or digit outside ASCII is kept whole, less
  the punctuation at its ends, and every term is matched as a quoted FTS5 phrase,
  inner quotes doubled (`fts_match`), so unicode61 splits and folds it as it did
  the indexed text: `café` is `cafe`, `स्मृति` three tokens (its vowel signs and
  virama are separators to unicode61), `記憶` one. Such a word is a term however
  short. Past 12 terms the rarest are still kept through fts5vocab: a word outside
  ASCII is looked up by the tokens SQLite's own tokenizer makes of it, read from a
  private in-memory FTS5 table (0.6 ms, only then). Every other word is stripped
  to `[A-Za-z0-9_]` as before, so an ASCII query asks for exactly what it did; a
  test holds the terms and the MATCH string to the old rule over 500
  punctuation-heavy and fuzzed queries, and on a copy of the reference index the
  keyword-only top 10 (sources, headings, scores) of 21 ASCII queries, ten of them
  punctuation-heavy, are identical to master's. `café naïve résumé` found 1 chunk
  by keyword there, now 10; `Narada नारद` now finds the mantra pages. Where it
  differs from the brief: internal ASCII punctuation is still glued as before
  (`e.g.` is `eg`, `O'Brien` is `obrien`, which FTS5 indexed as `o` and `brien`):
  quoting those too would change ASCII results, which the brief holds fixed. A
  word outside ASCII with punctuation inside (`l'été`, `crème"brûlée`) is a phrase.
- **H12.5 implemented** (same branch). `smriti.embed.provider` is the one
  embedding API: `embedder()`, `embed()`, `failure_reason()`,
  `in_process_embeddings(db=, rebuild=)`, `local_provider()`, `forget_provider()`,
  and `provider_keys()`, the one rule, which the indexer, search and the service
  all ask. **The rule:** the provider the index records, for everything that
  serves the existing index (searches, writes, repairs, the service, an
  incremental `smriti index`); `SMRITI_EMBED_PROVIDER` only for an index that
  records none, or a rebuild (`index_tree(full=True)`, so `SMRITI_EMBED_PROVIDER=X
  smriti index --full` migrates to X as before); else the auto order. A rebuild
  without the override keeps the recorded provider. An incremental run with a
  different override now indexes with the recorded model and warns once, where it
  refused ("run 'smriti index --full'") and an in-process search went
  keyword-only. One model is kept per process, as the cache did. A test holds the
  service, an in-process search or write and an incremental run to the rule for
  every pairing of record and override (12), and the migration to the full run
  alone. No private names across packages: besides the embedding names, the
  process probe is `smriti.core.procs.pid_alive` (logd and the embedding runtime
  took it from the day-log lock), `resolve_qmd_cmd`, `ENCODED_RE`, and
  `metrics.logging_to` for the eval sandbox's logger swap; the CLI's morning
  imports had gone with H12.2. `tests/test_module_boundaries.py` scans `src` and
  the profile (AST) for `from smriti.<a>... import _name`, `smriti.<a>..._name`
  and `m._name` through an alias, across top-level packages. `serverInfo.version`
  is `smriti.__version__` (a test holds it to pyproject's); `smriti_add_todo`
  says "whole phrase"; `search_with_status` builds its results and logs once, and
  its keyword leg fetches through `_fetch_candidate`.
- **H12.14 implemented** (same branch). `index_tree` is `_scan`, `_plan` (the
  purge, then what to read), `_embed_files` (read, chunk, embed, no lock held) and
  `_commit` (the one `BEGIN IMMEDIATE` transaction, reconciled against the disk),
  with every guarantee `tests/test_store.py` pins kept. It embeds in batches of
  `EMBED_BATCH = 512` chunks, closed at every 512 chunks of the run across files: a
  multiple of each in-process provider's own batch (onnx 32, local 512), so the
  model sees the same texts per call as when a run embedded everything at once.
  Batches are staged in memory up to one batch (an incremental run's usual few
  files), then in a temporary SQLite database on disk that SQLite deletes on
  close; the commit streams them through the one insert path. On 20,000 stub
  chunks Python's allocations peak at 19 MB (was 270 MB; working set 93 MB, was
  1.15 GB). `tests/test_index_batches.py`: the cap, a batched index equal to a
  one-call one, and the reconcile, the free write lock and a failed rebuild on the
  path that stages on disk; `tests/test_index_memory.py` (slow, about 2 min)
  indexes 101,000 chunks under a 64 MB ceiling. `smriti index --full` on copies of
  the reference tree (1,180 files, 19,888 chunks, the `local` model), alternating
  A B B A: master 163.2 and 81.4 s, this branch 149.2 and 82.6 s (the first pair on
  a busy machine); peak RSS 1,473-1,476 MB against 1,116-1,119 MB; the two indexes
  identical, chunks and vector bytes. With H12.5 this drops `NOT_YET` from
  `tests/test_broad_excepts.py`: the indexer's three re-raising `except
  BaseException` now give their reason.
- **The discovery file** (same branch; the follow-up `hardening/h12r` found).
  `read_discovery` tries a refused read (Windows' PermissionError while
  `os.replace` swaps the record in) again for up to 50 ms, in waits of 5, 10, 15
  and 20 ms; only a missing, still refused or malformed file is "no service".
  Tests simulate one refusal, four, and a lasting one. The other half of the same
  window, met once by the full suite (`test_one_service_however_many_clients_hold_it`):
  Windows refuses to delete the record while a client has it open to read, so an
  exiting service left a "draining" record with a dead pid; `clear_discovery`
  tries a refused removal again the same way.
- **Benchmark** (h12s; `scripts/bench.py`, 2026-11-07, the reference tree's copy,
  17 queries: the 10 before, four punctuation-heavy ASCII ones and three outside
  ASCII; two alternating rounds; A master `dccdea8`, B `43ffe44`, the last code
  commit before the rebase). Top 5 the same for all 14 ASCII queries and two of
  three others (`café naïve résumé` 4/5 shared: its keyword leg now finds
  something); the wake briefings identical (9,495 characters); 13 tools on both.
  The run met a busy machine (the reference install's embedding service loading
  its model, two voice workers and another branch's mypy started while it ran),
  and B's wake (1.47 and 5.19 s against 0.90 and 0.83), `smriti status` (1.51, 1.62
  s against 0.32, 0.34), CLI read (37, 45 s against 14, 13) and unchanged index (60,
  34 s against 21, 19) came out 2-5 times A's: out of all proportion to the changed
  code they run (the wake none; the unchanged index less than the 5-file index,
  which came out 22.48 / 22.64 s). So the flagged measures were taken again on the
  same copies, alternating: wake 895 / 894 ms (8 runs), `smriti status` 313 / 294 ms (8),
  unchanged index 38.4 / 22.2 s (4; A 49.2 and 27.7), CLI read 14.5 / 13.9 s (12),
  warm read p50 289 / 173 ms and p90 413 / 195 (4 sessions; A 170 and 408), write
  p50 163 / 73 ms (A 90 and 236), first query 776 / 774 ms. MCP initialize: 223 /
  279 ms in the run, 287 / 326 ms median over ten alternating starts each with
  spreads 172-827 and 175-397 ms, the same 137 modules imported: noise. No
  measure slower beyond noise.
- **H12.4 implemented** (2026-11-07, `hardening/h12w`; not yet reviewed or merged).
  `smriti.install.templates` is the one resolver: the package's copies under
  `smriti/_data/` (pyproject's force-include maps `narada/.smriti`,
  `memory_template` and `docs/PRIVACY.md` there), read through
  `importlib.resources`, else the checkout's own files, and those only when the
  package sits in a checkout's `src/`; nothing moved, so the editable reference
  install's auto-upgrade still compares against the checkout. The dispatcher moved
  into the package (`python -m smriti.install`; `scripts/install.py` is a shim). The
  build reads the version from `smriti.__version__` (pyproject's version is
  dynamic); `smriti --version` and the installer's `--version` and first line print
  it. The projects root is recorded in `<root>/.smriti/install.json`, a file of its
  own (privacy.json holds the two privacy answers, daylog.json the runtime config
  every process reads), and resolved: the flag; the record; for an install made
  before the record, the root its mirrors' `ai` and `knowledge` links point into
  when they agree, else `C:/Projects` when it exists; else none, and a new install
  links no mirrors until told. A read-only check on the reference install resolves
  `C:\Projects` from its mirrors. Claude Code's directory for a project is named as
  Claude Code names it (every character outside `[A-Za-z0-9]` made `-`, from the
  whole path), which for `C:/Projects` gives the old names but for `gpu_tests`: **a
  rerun there links one more project, `gpu_tests`, whose memory the literal
  `C--Projects-gpu_tests` never found.** `test_stub` (`smriti.llm.providers.stub`)
  gives canned answers, never auto-detected or listed, and raises unless
  `SMRITI_ALLOW_TEST_STUB=1`. CI's `wheel` job (Ubuntu, Windows) runs
  `scripts/wheel_smoke.py`: the built wheel with `[read]` in a fresh venv, run from
  the runner's temp directory without PYTHONPATH, home, root and runtime directory
  there; it checks the import location and version, the installer's template,
  scripts, mirror and record, write/read/status with the stub embedder, and a
  nightly that captures a planted transcript and digests it with `test_stub`.
  Locally (a venv from anaconda's Python): every step passes. **`pip install smriti`
  installs another project**: the name on PyPI is taken (project-vajra's smriti
  0.0.1), so the README installs from the repository (`pip install "smriti[read] @
  git+https://github.com/Stormbane/smriti.git"`). `project_template/` stays out of
  the wheel: only `scripts/setup_project.py`, a checkout script, uses it.
- **H12.9 implemented** (same branch). `smriti eval` runs a versioned quality set
  (`smriti.eval.quality`, version 1) beside the search cases: 26 cases over the
  synthetic corpus, which gains ten notes (now 30): 6 direct, 7 paraphrases (no word
  shared with their notes), 6 irrelevant (none of three letters or more shared with
  the corpus), 3 contradictions and 4 corrections; tests hold each rule. It reports
  hit@1/3/5 and MRR by kind, whether the newer of two notes is found (and first),
  and abstention: at the recall hook's 0.45 on the search score, and the
  threshold-free AUROC of the best search score and of the raw cosine between the
  query and its first result. With the real model here (onnx bge-m3 int8):
  answerable hit@1 70%, hit@3 and hit@5 100%, MRR 0.83; direct 100%; paraphrase
  hit@1 71%, hit@3 100%; contradictions resolved 3/3, newer first 2/3; **corrections
  resolved 1/4** (search has no notion of "later": an original usually outranks its
  correction); **no irrelevant query scores under 0.45** (each leg is normalised
  within its candidates, so the best result scores high whatever the query), score
  AUROC 0.85, raw-similarity AUROC 0.99: a threshold on raw similarity would
  abstain. The 14 search cases are unchanged by the larger corpus (13/14, MRR 0.96).
  `smriti eval --llm` digests two synthetic days through the nightly's own digest
  code and provider chain and scores coverage, facts stated and sentences traceable
  (lexical proxies, `smriti.eval.digests`); tested with a fake model, not run here
  against a real one. The weekly workflow (`.github/workflows/quality.yml`, Ubuntu,
  Mondays and on demand) caches the model, runs the needs_model tests (failing if
  one is skipped), and writes the scores to the job summary and an artifact. The
  wiring test's real model was `local` only, which the read extra doesn't install;
  it now takes the first installed one. Both needs_model tests pass here with onnx.
- **H12.16, the last of it, implemented** (same branch). Python 3.13 joins the
  matrix: every extra has 3.13 wheels on all three OSes (PyPI, 2026-11-07), and the
  sources compile under 3.13 with warnings as errors, so no job is
  continue-on-error. Branch coverage (`pytest-cov` in the dev extra,
  `[tool.coverage]`: `src/smriti`, vendored code left out) is reported on every job.
  The suite run the CI way on Windows, Python 3.12, gives 81.8% (lines 83.6%,
  branches 75.7%), and that job alone holds a floor of 79: each OS and Python takes
  other branches and skips other tests. The comments pass: in the day-log, embed,
  store, the LLM providers, the installer's privacy check, the witness and the
  deployed `backup.py`, comments that told an incident ("diff review P1", "recheck
  finding 2", "spec: review round 1, finding 6", "2026-09-06 ...") state the rule,
  with dates kept only for measurements that still stand; one named the maintainer.
- **Codex diff review of `hardening/h12w`** (2026-11-07): three P2s, fixed with
  regression tests that fail without the fix. A new install without
  `--projects-root` recorded nothing, so its rerun found the `wake.py` it had
  deployed, took itself for an install from before the record and linked
  `C:/Projects` unasked: the choice of none is recorded (`"projects_root": null`)
  and kept; only a record without the key predates it, and the reference install
  still resolves `C:\Projects` from its mirrors. A root given with `..` was kept
  as spelled, so Claude Code's directories were looked up under the wrong names:
  the root is normalised (`os.path.normpath`, not `resolve()`, which would follow
  junctions and change `C:/Projects`'s case). `smriti eval --llm --markdown`
  dropped the digests' scores and errors: the Markdown report has their table.
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

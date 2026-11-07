# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- A wheel installs and sets itself up without a checkout: it carries the memory
  template and the scripts the installer deploys (under `smriti/_data/`), and the
  installer runs as `python -m smriti.install` (`scripts/install.py` in a checkout
  runs the same). Install from the repository, `pip install "smriti[read] @
  git+https://github.com/Stormbane/smriti.git"`: `smriti` on PyPI is another
  project. CI builds the wheel, installs it in a fresh venv and runs the installer,
  write, read, status and one nightly from it, on Ubuntu and Windows.
- `smriti --version` and `python -m smriti.install --version`. The build reads the
  version from `smriti.__version__`, the one place it is written.
- `smriti eval` reports a versioned quality set over its synthetic corpus (version
  1, 26 cases): retrieval by kind of question (direct, paraphrase, contradiction,
  correction; hit@1/3/5, MRR), whether the newer of two disagreeing notes wins, and
  abstention on irrelevant queries (at a fixed threshold, 0.45, and threshold-free).
  `--markdown` prints the scores as tables. `smriti eval --llm` also digests
  synthetic days with the nightly's LLM and scores each digest's coverage of its
  turns, the facts it states and the share of its sentences traceable to the turns;
  it calls a real model, so it runs locally only.
- A weekly CI workflow runs the `needs_model` tests and the quality set with the
  real embedding model, cached between runs, and puts the scores in the job summary.
- `test_stub`, an LLM provider with canned answers for tests and the CI smoke run;
  it refuses to run unless `SMRITI_ALLOW_TEST_STUB=1`.
- CI runs Python 3.13 too, and measures branch coverage, with a floor.

### Changed

- The nightly's codex fallback is opt-in: `"nightly_fallback": "codex_cli"` in
  `daylog.json`, or `SMRITI_NIGHTLY_FALLBACK`, which wins when set (empty: none).
  Without either, a day Claude Code fails gets no digest that night, and the next
  nightly retries it. An install that relied on the fallback must set the key; the
  reference install does.
- An append to a daily file keeps the file's line ending. Since H1.4's binary
  append, new entries were LF beside CRLF ones on Windows; a new file gets the
  platform's, as before H1.4.
- The credential scrub is about 30 times faster on ordinary text (50 kB: 19 ms to
  0.6 ms), with the same masks.
- Digests and rollups record `prompt_fp`, a fingerprint of their prompts, budgets,
  provider chain and the two names, and rollups `members_hash`, a manifest of their
  members' content; a different one makes them stale, and the next nights redo
  them within the usual caps. Those written before have neither and are judged by
  the old rule until they are next regenerated, so upgrading redoes nothing.
- A failed backup commit or push shows: `smriti status` prints the last commit and
  push from `.git/smriti-backup.jsonl` and since when pushes fail, the nightly
  records them as a `backup` step that fails the night, and `smriti backup` prints
  what it did (still exiting 0). A push with nowhere to go isn't a failure. The
  deployed `backup.py` tags its log lines with the caller's run; reinstall to get it.
- One rule chooses the embedding provider, for the indexer, search and the
  embedding service alike: the provider the index records, for everything that
  serves the index; `SMRITI_EMBED_PROVIDER` only for an index that records none,
  or for `smriti index --full`, which migrates the index to it as before. An
  incremental `smriti index`, a search or a write with a different override set
  now uses the recorded model and warns once, where the index run refused and an
  in-process search went keyword-only.
- `smriti index` embeds in batches of 512 chunks and stages them on disk until
  its one write transaction, so a full index's memory stays flat (20,000 chunks:
  a 93 MB working set, was 1.15 GB). The index it writes is the same.
- The MCP server reports the package's version in `serverInfo` (it said 0.1.0).
- The installer's projects root is recorded in `<root>/.smriti/install.json` and
  reused, instead of defaulting to `C:/Projects` on every run. A new install links
  no project mirrors until given `--projects-root`; an install made before the
  record keeps the directory its mirrors point into (or `C:/Projects`, the old
  default, when it has no mirrors and that directory exists), and records it.
- `smriti.core` imports nothing from the packages above it: the credential scrub
  is `smriti.core.scrub` and the day zone `smriti.core.dayzone`.
  `smriti.daylog.scrub` and `smriti.daylog.model` re-export them, so existing
  imports keep working. A test fails on an import against that direction.
- The lock transition is over: a named lock (file edits, the day-log writer, the
  handoff, the nightly run) is one file in `<runtime>/locks`, and no longer also
  its old file in the shared temp directory. `SMRITI_LEGACY_LOCKS` is gone.
  Restart smriti's long-lived processes (MCP servers, logd) after upgrading:
  one still running code from before 0.2 holds only the old file, and is no
  longer excluded. A runtime directory that is refused or can't be made fails
  closed, as it did with the old files off: the lock isn't taken.
- On Linux and macOS the runtime directory is `~/.cache/smriti/run` whatever
  `XDG_RUNTIME_DIR` says, so a cron job and a desktop session take the same lock
  files (it was `$XDG_RUNTIME_DIR/smriti` when that was set). A desktop install
  that had it set moves there once: restart smriti's processes after upgrading,
  and its embedding service starts afresh with a new key. Windows is unchanged
  (`%LOCALAPPDATA%\smriti\run`). `SMRITI_RUNTIME_DIR` still overrides it; set
  it for every process or none.
- The agent doc the installer writes (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`)
  no longer says memory arrives when a file is touched; rerun the installer to
  refresh it.
- A memory root with no `.smriti/wake-context.md`, as every new install is
  (nothing creates it), wakes with an identity section: `identity.md` then
  `mind/mind.md`, as they are. A root that has the file wakes as before.
- `SMRITI_CLAUDE_TIMEOUT` sets the `claude -p` timeout, in seconds (default
  300); `NARADA_CLAUDE_TIMEOUT`, its old name, is still read when it isn't set.
- Memory text is fsynced: every write through `smriti.core.memwrite` (entries,
  notes, todos, findings, decisions, feature specs, the handoff, the outbox,
  renders, digests and rollups) and every write of the morning's ledger is on
  the disk before the call returns, with its directory on POSIX. The `index.md`
  listings, the index and state files aren't. It costs about 0.8 ms a write at
  the median, and now and then a flush that waits for the disk.
- A nightly that can't open its lock file doesn't run: it skips, fails (exit 1),
  and its status, which the wake shows, says why. It ran without exclusion
  before.

### Removed

- The encrypted private store (`smriti.private`, `private/` in the tree), with the
  `private` extra and `cryptography` in `dev`. Nothing wrote to it, yet every CLI
  command but `eval` and `embed` initialised it. `smriti status` loses its
  committed-key warning. A tree that has one keeps it out of the index,
  and the template's `.gitignore` still lists `private/.key`; read its notes with
  0.2.0 and that key (PRIVACY.md, "The private store (removed)").
- qmd as a recall backend: `smriti recall index` and `smriti recall daemon`, the
  `SMRITI_RECALL_BACKEND`, `_QMD_URL`, `_NO_HTTP`, `_COLLECTION` and `_INTENT`
  settings, logd's ten-minute keepalive of qmd's daemon, and the installer's
  daemon start with its `--skip-recall-daemon` flag. A qmd daemon already running
  keeps running until stopped (`qmd mcp stop`).
- The PostToolUse ambient-recall hook (`smriti.recall`) and the `smriti recall`
  command. The installer no longer wires the hook or deploys its script
  (`associative_recall.py` for Claude Code, `recall_hook.py` for Codex), and
  `smriti doctor` no longer checks Codex's copy. An entry an earlier install
  wired stays until removed by hand; its script now exits without doing
  anything. `examples/python_agent.py` searches through `smriti.store.search`.
- `smriti.daylog.presence`, which only the hook used.

The code of each is in the git history.

### Fixed

- A failed `git status` (an unreadable index, say) read as "nothing to commit":
  backups stopped while each run, `smriti status` and the nightly said all was
  well. The backup script logs it as a failed commit (still exiting 0), and logs
  "nothing to commit" and "up to date" as well; a run is reported a success only
  from the lines that show it, so a run that logged no outcome is "unknown", not
  ok. Reinstall to get the new script; until then an older one's quiet runs read
  as "unknown".
- The installer rewrote `~/.claude.json` and `~/.claude/settings.json` (and
  Codex's `config.toml`) with a plain write, under no lock, and kept one `.bak`
  that every run overwrote, losing the backup of the original. Each edit now holds
  a lock from its read to its write and reads the file again just before writing:
  a change Claude Code made meanwhile is kept (the edit is redone on it once, or
  refused, and the install exits nonzero). It writes atomically, with the file's
  permissions and through a symbolic link, after copying the file as it was to a
  new `<file>.smriti-<UTC time>.bak`; the first of these is never removed, and
  five are kept. The home is read when a step runs, not when the module is
  imported.

- The installer looked for a project's Claude Code memory as `C--Projects-<name>`;
  it now names the directory as Claude Code does, from the project's whole path
  with every character outside `[A-Za-z0-9]` made `-`, so a project under any root,
  or with `_` or `.` in its name, has its memory linked. Nor does it warn any more
  that POSIX symlinks aren't implemented: they are.
- Keyword search dropped every letter outside ASCII from a query: `café` searched
  for `caf`, and a Devanagari or CJK word for nothing, so a query of such words
  found nothing by keyword, and nothing at all in keyword-only mode. Such a word
  is now kept and matched as FTS5 tokenizes the indexed text. An ASCII query
  searches for exactly what it did.
- On Windows a client could read the embedding service as not running while the
  service replaced its discovery file (a refused open), and start a second one or
  fail a query; a refused read is now tried again for up to 50 ms. An exiting
  service's removal of the file, refused while a client read it, is too.

- A manual `smriti nightly` and the scheduled one could run at once. One run holds
  a lock for the memory root; a second waits 10 seconds, then skips without
  touching the status file.
- A turn appended between a day's render and its digest was counted in the
  digest's hash but missing from its text, and a later render could make the pair
  look current: the turn never reached a digest. Render and digest now work from
  one read of the day.
- A digest regenerated from the same turns left its week's rollup current, and a
  rollup's manifest was taken after its LLM call, so a member changed during the
  call passed as read.
- The nightly's status file and the witness proof were written through temp files
  another writer could share.

- The morning message falls back again when Claude Code fails, if the nightly's
  fallback is on: from the digest alone, without web search. Since `d2e0518` a
  failed compose sent nothing that day. `smriti morning --dry-run` falls back the
  same way.

- A day-file the nightly could read only partway was rendered and digested from
  what was read, replacing the day's render and digest with part of them. It now
  publishes nothing from such a read: the render and digest stay, the status and
  the wake name the day, and the next night reads it again.
- A digest's `tz_offset` was `None` for a named zone and, for the system's zone,
  the offset on the night it ran rather than on the day it digests. It is the
  digested day's now; a zone behind UTC is written with a sign (`-5:00:00`).
- `smriti_write_handoff` could say it overwrote a letter a wake had just
  archived.

### Security

- Every write of memory text into the tree is masked at one chokepoint,
  `smriti.core.memwrite`: besides `smriti_write`, the project tools (findings and
  their cross-project stubs, decisions, feature specs and status notes, todos),
  the handoff, the reference profile's outbox, rendered days, digests, rollups and
  `index.md` listings, titles and metadata included. A feature slug shaped like a
  credential is refused. A test scans the source and fails on a file write
  outside the chokepoint and an allowlist of runtime and index state.
- The codex fallback is off by default (above): its read-only sandbox stops writes
  and the network but not reads, while it reads untrusted transcripts.
- The credential scrub masks quoted keys, as JSON, YAML, TOML and Python write
  them: `{"password": "..."}`, `'token':'...'`, escaped JSON (`\"secret\":
  \"...\"`) and JSON escaped more deeply, and keys that end in a secret's name
  (`access_token`, `client-secret`, `db.password`, `accessToken`). A quoted value
  is masked whole, up to the quote that closes it at its own level of escaping, so
  an escaped quote inside it doesn't end it. Ordinary text costs what it did.
- An older private store kept its key in `private/README.md`, which is committed.
  smriti now moves such a key to `private/.key` (the same key), takes it out of
  the README, and warns that it is still in git history and should be rotated;
  `smriti status` repeats it until `private/KEY-WAS-COMMITTED.txt` is deleted.
  `docs/PRIVACY.md` has the steps. The move happens once, under a lock, with
  `.key` in `private/.gitignore` before the key is written.
- The nightly's status file (`log/.nightly-status.json`, which backups commit and
  push) held provider errors as they were, so a credential a CLI's error quoted
  reached the remote. Every string in it is masked, as the morning's status was.

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

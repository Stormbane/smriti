# Security and privacy

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/Stormbane/smriti/security/advisories/new)
when available. Otherwise, open an [issue](https://github.com/Stormbane/smriti/issues/new)
labelled `security` (or ask a maintainer to apply that label). In a public issue,
describe the affected component and impact without exposing credentials, private
memory, or an exploit against a live installation. Include the version or commit,
platform, and a minimal reproduction using synthetic data in a private report.

smriti is alpha software. Report against the current code; there is no maintained
security backport series or guaranteed response time.

## Threat model

- The local agent and operator are trusted, but transcripts, retrieved documents,
  and other inputs may contain prompt injections. MCP runs over local stdio with
  the launching user's filesystem permissions; it is not a sandbox for hostile
  agents. `smriti.core.safe_names` validates MCP `project`, `slug`, and `file`
  arguments as single path segments, including Windows device-name checks. Nested
  write branches have a separate containment check in the writer, which also
  refuses an entry whose dated directory or day file resolves outside the tree
  (a junction or symlink at `notes/2026`, say). These checks
  prevent path traversal; they cannot decide whether an otherwise valid request
  was induced by a prompt injection.
- The ordinary memory tree is local **plain text**. Its SQLite search index also
  contains memory text. Anyone with filesystem access can read it. Local embedding
  providers keep inference local after model installation; choosing a hosted
  provider sends embedding inputs to that provider. Nightly digests and rollups
  send conversation text to the configured LLM CLI/provider.
- Those LLM calls, and the morning message's, read that untrusted text. On
  Claude Code they run with no tools, MCP servers or skills
  (`tests/test_llm_tool_boundary.py` checks the flags against the installed
  CLI), so an injection can change what they write but can't act. The
  fallback, `codex exec`, is off unless configured (`"nightly_fallback":
  "codex_cli"` in `daylog.json`, or `SMRITI_NIGHTLY_FALLBACK`, which wins when
  set): its read-only sandbox stops writes and the network, not reads, so an
  injection could have it read any of the user's files into a digest that is
  committed and pushed. When it is on, the morning falls back to it too, from
  the digest alone and without web search. With `morning_web_search` on, the
  morning call can search the web, and an injected instruction could leak
  digest text through its search queries. It is off by default.
  [`docs/PRIVACY.md`](docs/PRIVACY.md#what-goes-to-an-llm) has the details.
- The embedding service listens on a loopback TCP port, and every process of the
  same user that holds its key can use it; other users can connect but are
  refused. See [The embedding service](#the-embedding-service).
- The locks that serialise smriti's own writers (file edits, the day-log writer,
  the handoff letter, the nightly run) are OS locks on files in the runtime
  directory's `locks/`, which other users can't create or hold. Every process of
  the user resolves the same runtime directory whatever its environment (it
  doesn't follow `XDG_RUNTIME_DIR`, which a cron job lacks), so all of them take
  the same files; `SMRITI_RUNTIME_DIR` must be set for all of them or for none.
  When the directory is refused (another user owns it, or others can write to
  it) or can't be made, a lock isn't taken anywhere else: what needed it fails
  rather than running unguarded. A file edit says its lock is unavailable and
  why, the nightly skips and says so, and the day-log writer and the handoff
  letter fail as busy (see `smriti.core.oslock`).
- Backups commit the tree and can push it to a git remote you choose, in plaintext
  unless git-crypt encrypts it. Use a private remote. We recommend git-crypt for
  sensitive memory backups, but **the installer does not set up git-crypt**: it asks
  whether the remote is private, reports the day-log as encrypted only when the
  git-crypt filter is configured and `git-crypt status` confirms it (a
  `.gitattributes` line alone encrypts nothing), and prints the setup steps when the
  remote isn't confirmed private. Git filenames,
  commit messages, and other metadata remain visible even when file contents are
  encrypted. [`docs/PRIVACY.md`](docs/PRIVACY.md#what-is-pushed-in-the-clear) lists
  what is pushed in the clear.
- smriti runs no code from the memory tree, which agents write to and a restore
  brings back from git. The SessionStart hook runs `<python> -P -m smriti.wake`
  and `smriti backup` (the hourly task, and the nightly's witness step) runs
  `<python> -P -m smriti.backup`: the installed package's code, by the
  interpreter it is installed in. `-P` keeps the current directory off the
  import path, so a session started in the tree can't import from it. The
  installer copies one script into `<root>/.smriti/`, `narada-p.sh`, which only
  a person runs; a reinstall removes the `backup.py` earlier installs copied
  there, and their `wake.py` once no hook smriti manages still runs it (a
  hand-written one is left, and nothing runs it). What `daylog.json` configures
  is the operator's: `notify_cmd` is run by the morning message and the
  reference profile's `tell_suti`, and without one the reference profile runs
  `<root>/.smriti/hooks/notify_suti.py`, a script in the tree; keep such a
  command out of the tree to keep the tree free of code that runs.
- A profile (`"profile"` in `daylog.json`) is code from an installed package,
  found through the `smriti.profiles` entry-point group of the Python
  environment smriti runs in; nothing is loaded from the memory tree, so the
  config can only choose among packages the operator installed. smriti's own
  wheel carries no profile. The current directory and the running script's
  directory are not searched. What the lookup finds is cached in the private
  runtime directory (`profiles-*.json`, where other users can't write), keyed on the import
  path's entries and their modification times; a missing, corrupt or stale
  cache means a fresh scan.
- The models smriti chooses (the embedding providers' defaults,
  `gpahal/bge-m3-onnx-int8` and `all-MiniLM-L6-v2`, and the reranker,
  `Alibaba-NLP/gte-reranker-modernbert-base`) load at the Hugging Face Hub
  commits pinned in `src/smriti/_vendored/memsearch/revisions.py`, from the
  cache and from the Hub alike, never at whatever the repository's `main` holds.
  A commit's hash names its content: a repository can add commits, or rewrite
  its history so that the pinned one is gone (a download then fails), but a
  change upstream doesn't change the weights and tokenizer smriti runs or the
  vectors its index holds. The sentence-transformers paths (the `local`
  provider, the reranker's torch backend) run no code from a model's repository
  (`trust_remote_code` is off). The Hub itself, and the libraries that read
  the files (onnxruntime, tokenizers, transformers), are trusted.
- `core/scrub.py` masks recognised credential patterns before captured turns
  are persisted, and `smriti.core.memwrite` applies it to every write of memory
  text into the tree: entries, the project tools' files, the handoff, the
  reference profile's outbox, rendered days, digests, rollups and `index.md`
  listings, titles and metadata included. `tests/test_memwrite.py` fails on a
  file write anywhere else in the source outside an allowlist of runtime and
  index state. This is pattern-based, not comprehensive secret or personal-data
  detection. Unrecognised secrets and ordinary personal information remain, and
  files edited by hand are not scrubbed.

## What leaves the machine

Every way smriti sends data off the machine: what goes, to whom, when, and how to
stop it. Nothing else in smriti opens a network connection; the embedding service
listens on loopback only.

- **The agent's own session.** The wake briefing, and whatever an MCP tool
  returns (search results, a read note), enter the agent's context, which its
  harness (Claude Code, Codex) sends to its model provider with the rest of the
  session. *When:* each session start, and each tool call the agent makes.
  *Off:* remove the SessionStart hook and the MCP server from the harness's
  config.
- **Digests and rollups, to an LLM.** Each day's rendered conversation log
  (everything capture keeps, credential shapes masked) goes to the digest call,
  and the digests to the week, month and year rollups. *To:* `claude -p`, under
  your Claude Code login (Anthropic). `SMRITI_NIGHTLY_PROVIDER` can name another
  provider: `codex_cli` (OpenAI, through `codex exec`), `anthropic_api` or
  `openai_api` (the API with your key; `OPENAI_BASE_URL` moves the latter), or
  `ollama` (`SMRITI_OLLAMA_URL`, local by default). A fallback only when
  `"nightly_fallback"` in `daylog.json` or `SMRITI_NIGHTLY_FALLBACK` names one
  (`codex_cli` in practice). *When:* the nightly (`smriti nightly`, 03:00), for
  at most 10 days (`--digest-cap`) and 2 rollups (`--rollup-cap`) a night; a
  long day goes in parts and a merge. *Off:* both caps at 0, or no `smriti-nightly`
  task. Turning capture off stops new collection only: the nightly still digests
  the day-logs already on disk that have no current digest, and rolls them up.
- **The morning message.** Yesterday's digest, at most 20,000 characters, goes
  to `claude -p` (and to the fallback, when one is set and `claude -p` fails);
  the message it composes goes to your `notify_cmd`, on its stdin, and from
  there wherever that command sends it. *When:* the `smriti-morning` task
  (07:00), once a day; `smriti morning --dry-run` composes one without sending
  it. *Off:* no `notify_cmd` in `daylog.json`: then the task composes and sends
  nothing. Or no `smriti-morning` task.
- **Web search, off by default.** With `"morning_web_search": true`, the morning
  call can search the web for headlines: its queries, which the model writes and
  an injected instruction could fill with digest text, go to Claude Code's
  WebSearch tool and the search provider behind it. *Off:* leave it false. The
  fallback never gets it.
- **Backups, to the git remote.** Everything git tracks in the tree: memory
  notes, captured conversations and their renders, digests, rollups,
  `daylog.json` and witness manifests, in plaintext unless git-crypt encrypts it
  (file names, sizes and commit messages stay readable either way). *To:* every push URL of the current branch's
  upstream remote, or of `origin` when it has none. *When:* hourly (the
  `smriti-backup` task, when something changed), in the nightly's witness step,
  and on `smriti backup --push`. *Off:* no remote to push to (remove the
  upstream's remote and `origin`): the backup then commits on this machine only.
  [`docs/PRIVACY.md`](docs/PRIVACY.md#what-is-pushed-in-the-clear) lists what is
  pushed.
- **Timestamps, to the OpenTimestamps calendars.** One hash: the SHA-256 of the
  night's manifest (a small file naming the commit) with a random nonce
  appended, hashed again, so the calendars never see the manifest's own hash.
  It goes to four public calendars (`smriti.witness.CALENDARS`); later nights
  ask the calendar named in a pending proof, one of four known ones, for its
  Bitcoin attestation. `smriti witness verify` asks mempool.space, then
  blockstream.info, for the anchoring block by its height. *When:* the
  nightly's witness step, and `verify` when you run it. *Off:* `smriti nightly
  --no-witness` (change the scheduled task's command), and `verify --offline`.
- **Models, from the Hugging Face Hub.** The embedding model
  (`gpahal/bge-m3-onnx-int8`, about 560 MB) is downloaded the first time it is
  needed; later loads read the cache without a request. `smriti read` also
  reranks with `Alibaba-NLP/gte-reranker-modernbert-base`: downloaded on first
  use, and its file list asked of the Hub on every run. Both are fetched at a
  pinned commit (see the [threat model](#threat-model)). The MCP server's search
  doesn't rerank. The requests name the model, and the library's user agent its
  versions; no memory text goes. *Off:* `HF_HUB_OFFLINE=1` (the default `onnx`
  provider's cached model still loads, and the reranker fails and the combined
  scores stand; [`docs/PRIVACY.md`](docs/PRIVACY.md#other-network-requests) has
  the `local` provider's case), or `smriti read --no-rerank`.
  `HF_HUB_DISABLE_TELEMETRY=1` is the library's own telemetry switch.
- **A hosted embedding provider, off by default.** `SMRITI_EMBED_PROVIDER` set
  to `openai`, `google` or `voyage` sends every chunk of the indexed tree, and
  every search query, to that API. The index records its provider, and later
  runs keep using it without the variable. *Off:* leave it unset, or rebuild with
  `SMRITI_EMBED_PROVIDER=onnx smriti index --full`.
- **The reference profile's channels** (only with `"profile": "reference"`). The
  `tell_suti` tool's text, with its level, goes to the fan-out sender
  (`notify_cmd`, else `<root>/.smriti/hooks/notify_suti.py`), which sends the
  levels `message`, `error` and `emergency` to Telegram, and every level to the
  maintainer's app feed and its push notifications; that script, not smriti,
  does the sending. The morning message goes through it too when `notify_cmd`
  names it. *When:* each time the agent calls the tool. *Off:* no profile, or
  neither command.
- **Local by default.** Ollama, for the LLM (`SMRITI_OLLAMA_URL`) or embeddings
  (`SMRITI_EMBED_PROVIDER=ollama`, `OLLAMA_HOST`), is on localhost unless
  pointed elsewhere; then the same text goes there.

The LLM CLIs and harnesses talk to their own vendors under their own settings
and terms, telemetry included; smriti doesn't change those.

## The private store (removed)

Versions before 0.3 had `smriti.private`, an append-only store of notes
encrypted with Fernet, in `private/` in the tree, with its key in plain text
beside it in `private/.key`. Nothing in smriti wrote to it, no command or tool
read it, and it hid notes from the git remote only, never from anyone with the
machine, so it was removed. A tree that still has one keeps it out of the
search index, and the memory template's `.gitignore` lists `private/.key`. Its
notes can be read with smriti 0.2.0 (`git checkout v0.2.0`) and that key; back
the key up outside the tree, because git can't restore it.
[`docs/PRIVACY.md`](docs/PRIVACY.md#the-private-store-removed) says more.

## The embedding service

`smriti embed serve` (`smriti.embed`) holds the embedding model for one memory
root and embeds text for the user's other smriti processes: the MCP servers,
`smriti read`, hooks. What it exposes, and to whom:

- **The endpoint.** A TCP listener on `127.0.0.1`, on an ephemeral port, never on
  another interface. On Windows it binds with `SO_EXCLUSIVEADDRUSE`, so no other
  process can bind the same port alongside it. Its port, pid and instance id are
  in a discovery file in the runtime directory.
- **The runtime directory** holds the key, the discovery files, the services'
  locks and logs, and smriti's other locks (`locks/`), and is never inside the
  memory tree, so none of it is committed
  or pushed: `%LOCALAPPDATA%\smriti\run` on Windows, private to the user by the
  profile's ACLs; `~/.cache/smriti/run` elsewhere, whatever `XDG_RUNTIME_DIR`
  says, created mode 0700 and refused when another user owns it or others can
  write to it (smriti doesn't check ownership on Windows; it relies on the
  profile's ACLs, including when `SMRITI_RUNTIME_DIR` points elsewhere).
- **The key.** 32 random bytes in `<runtime>/embed.key`, made once by an exclusive
  create (mode 0600 on POSIX, refused if others can read it) and never replaced.
  One key serves all of a user's memory roots.
- **Authentication, both ways.** Each connection starts with an HMAC-SHA256
  challenge-response over the key. The client checks the service's proof before it
  sends anything else, so a process that took the port after the service died
  learns only a random nonce, never the text. The service checks the client's
  proof before it reads a request, so a peer without the key gets no answer and no
  embedding. Messages are length-prefixed JSON (never pickle), at most 4 MB, and at
  most 4 KB before authentication.
- **Bounds.** At most 32 connections at once (more are closed on accept), a 1 s
  handshake, 64 queued requests, and a deadline on every read and write. An
  authenticated connection may carry further requests, one at a time, and is
  closed 10 s after its last reply (`keepalive_s`, advertised in the hello) or
  as soon as the service drains; it holds its slot while it lives, and only a
  peer with the key gets that far. With
  these bounds another local user can tie up the port for a while, which degrades search to
  keywords and leaves new writes' vectors pending, but can't exhaust memory or
  threads, or make a client hang.
- **Holds.** While an MCP server runs, from its first search or write on, it
  sends a `hold` every `idle_s / 3` (5 minutes by default), each on a connection
  of its own, so the model stays loaded while sessions are open; the service
  exits `idle_s` after the last request or hold. A hold is a request like any
  other: sent only after authentication, under the same bounds, and it does no
  work but reset the idle timer (its answer is the instance, its state and
  `idle_s`). Before authentication the hello gains only a constant `"hold": true`;
  nothing new is read. A peer with the key can keep the model in memory for as
  long as it likes, as it could by sending requests. Holds never stop a drain:
  `smriti embed stop` and an index migration still end the service, and a
  holding server then starts the next one.
- **The trust boundary is the user.** Any process running as the same user can
  read the key and use the service, and could equally read the memory tree
  itself; the service adds no access the user's own processes lack. The traffic
  is not encrypted: it never leaves the machine, but anyone who can capture
  loopback traffic (an administrator) can read the texts being embedded.
- **The off switch.** `SMRITI_EMBED_SERVICE=0` turns the service off; every process
  then loads the model itself, and nothing listens.

## Privacy defaults

Search telemetry records a truncated SHA-1 query hash and query length instead of
query text. `SMRITI_LOG_QUERIES=1` opts into plaintext query logging. Hashes permit
correlation and guesses of known queries; they are not encryption. Metrics also
include operational metadata such as source paths and timings.

The installed memory-template `.gitignore` excludes the search index, SQLite
side files, the metrics log (and the recall log earlier versions wrote), LLM
work directories, the parked queue, day-log runtime state, locks, and the key
of the private store earlier versions had (`private/.key`). It does **not**
exclude memory notes, captured day logs, digests, or rollups. Ignore rules do not untrack files
already committed; check existing repositories before enabling remote backups.
The search index excludes runtime state, and `private/` (that store) where a
tree still has it.

Capture is off until it is configured, and is chosen at install.
`<memory-root>/.smriti/daylog.json` turns it on with `"capture": true` (the default
sources: Claude Code transcripts, Codex rollouts, and voice transcript files) or a
`"sources"` list of its own; without either, or without the file, nothing is
captured, and `smriti logd` and `smriti status` say so. The installer asks
whether to capture conversations, or takes `--capture` (which writes
`"capture": true`) / `--no-capture` (which writes `"capture": false`, capturing
nothing, `extra_sources` included); with neither a terminal nor a flag, a new memory
root captures nothing, and an existing one keeps what its `daylog.json` says
(`extra_sources` still adds explicit sources, unless it says `"capture": false`). There is
no blanket anonymisation of captured conversations; captured turns, and everything
smriti writes into the tree, are scrubbed of credential-shaped strings.

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
  write branches have a separate containment check in the writer. These checks
  prevent path traversal; they cannot decide whether an otherwise valid request
  was induced by a prompt injection.
- The ordinary memory tree is local **plain text**. Its SQLite search index also
  contains memory text. Anyone with filesystem access can read it. Local embedding
  providers keep inference local after model installation; choosing a hosted
  provider sends embedding inputs to that provider. Nightly digests and rollups
  send conversation text to the configured LLM CLI/provider.
- The embedding service listens on a loopback TCP port, and every process of the
  same user that holds its key can use it; other users can connect but are
  refused. See [The embedding service](#the-embedding-service).
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
- The optional private store encrypts content with Fernet. New stores put their
  key in `private/.key`; it must never be committed and is excluded by both the
  memory template and the private store's `.gitignore`. Back it up separately.
  Local code with access to the key can decrypt the store; consent tokens prevent
  accidental access, not a malicious operator. Legacy stores can still read a key
  from `private/README.md`: that older format needs manual migration and, if the
  key reached a remote, key rotation and history cleanup.
- `daylog/scrub.py` masks recognised credential patterns before captured turns
  are persisted. This is pattern-based, not comprehensive secret or personal-data
  detection. Unrecognised secrets and ordinary personal information remain; direct
  memory writes do not pass through this capture scrubber.

## The embedding service

`smriti embed serve` (`smriti.embed`) holds the embedding model for one memory
root and embeds text for the user's other smriti processes: the MCP servers,
`smriti read`, hooks. What it exposes, and to whom:

- **The endpoint.** A TCP listener on `127.0.0.1`, on an ephemeral port, never on
  another interface. On Windows it binds with `SO_EXCLUSIVEADDRUSE`, so no other
  process can bind the same port alongside it. Its port, pid and instance id are
  in a discovery file in the runtime directory.
- **The runtime directory** holds the key, the discovery files, the services'
  locks and logs, and is never inside the memory tree, so none of it is committed
  or pushed: `%LOCALAPPDATA%\smriti\run` on Windows, private to the user by the
  profile's ACLs; `$XDG_RUNTIME_DIR/smriti` or `~/.cache/smriti/run` elsewhere,
  created mode 0700 and refused when another user owns it or others can write to
  it (smriti doesn't check ownership on Windows; it relies on the profile's ACLs,
  including when `SMRITI_RUNTIME_DIR` points elsewhere).
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
  handshake, 64 queued requests, and a deadline on every read and write, so
  another local user can tie up the port for a while, which degrades search to
  keywords and leaves new writes' vectors pending, but can't exhaust memory or
  threads, or make a client hang.
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
side files, metrics and recall logs, LLM work directories, the parked queue,
day-log runtime state, locks, and `private/.key`. It does **not** exclude memory
notes, captured day logs, digests, or rollups. Ignore rules do not untrack files
already committed; check existing repositories before enabling remote backups.
The search index excludes the private store and runtime state.

Capture is off until it is configured, and is chosen at install.
`<memory-root>/.smriti/daylog.json` turns it on with `"capture": true` (the default
sources: Claude Code transcripts, Codex rollouts, and voice transcript files) or a
`"sources"` list of its own; without either, or without the file, nothing is
captured, and `smriti logd` and `smriti status` say so. `scripts/install.py` asks
whether to capture conversations, or takes `--capture` (which writes
`"capture": true`) / `--no-capture` (which writes `"sources": []`); with neither a
terminal nor a flag, a new memory root captures nothing, and an existing one keeps
what its `daylog.json` says (`extra_sources` still adds explicit sources). There is
no blanket anonymisation of captured conversations; captured turns and
`smriti_write` entries are scrubbed of credential-shaped strings.

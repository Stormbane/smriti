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

Capture is chosen at install. `scripts/install.py` asks whether to capture
conversations, or takes `--capture` / `--no-capture`; with neither a terminal nor a
flag, a new memory root captures nothing, and an existing one keeps the sources its
`daylog.json` has. When capture runs, its default sources are Claude Code
transcripts, Codex rollouts, and voice transcript files. Configure
`<memory-root>/.smriti/daylog.json` to limit these sources; `"sources": []` (what
`--no-capture` writes) disables all defaults (`extra_sources` still adds explicit
sources). There is no blanket anonymisation of captured conversations.

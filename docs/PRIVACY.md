# smriti — Privacy

**Audience**: operators deploying smriti, and anyone working on it, who
want to know what of the memory leaves the machine, where it goes, and
how to keep it unreadable there.

---

## What is pushed in the clear

The backup (`<root>/.smriti/backup.py`, run by the hourly `smriti-backup`
task and by the nightly witness step) runs `git add -A`, commits, and
pushes the current branch to its upstream (`origin` and the same branch
name when it has none yet; a detached HEAD isn't pushed): to every push
URL that remote has, not only the first. Whatever git tracks reaches the
remote as plain text, unless
git-crypt encrypts it first. **smriti does not set up git-crypt.** With
the memory template's `.gitignore`, that is:

- **Your conversations, when capture is on.** Capture is off until
  `.smriti/daylog.json` says `"capture": true` or lists `"sources"`.
  Then `log/YYYY/MM/DD.jsonl` and its render `DD.md` hold every message
  you typed and every reply the agent wrote in Claude Code, Codex and the
  voice channel. Tool calls,
  tool output and thinking are dropped. `core/scrub.py` masks strings
  shaped like credentials (provider API keys and tokens, JWTs, bearer
  headers, `password=` style assignments, passwords in URLs, private key
  blocks); everything else stays, including names, pasted file contents
  and personal details.
- **What the nightly derives from them:** `DD-digest.md` and the week,
  month and year rollups, masked as they are written, like the render.
- **Everything written to memory:** `identity.md`, `mind/`, `people/`,
  `open-threads/`, `journal/`, `projects/`, `notes/`, `sources/` and
  `semantic/`. Everything smriti writes into the tree goes through the
  same credential scrub as captured turns (`smriti.core.memwrite`): what
  `smriti_write` (or `smriti write`) saves, the project tools' findings,
  decisions, feature specs and todos, the handoff, the `index.md`
  listings, and the nightly's files. Files you edit directly don't.
- **Configuration and session files under `.smriti/`:** `daylog.json`
  (names, `location`, `notify_cmd`), `privacy.json` (the installer's
  record of your answers), `wake-context.md`, a pending handoff and its
  archive, and the scripts `wake.py` and `backup.py`.
- **Witness manifests** under `witness/`.

Never pushed, because the template's `.gitignore` excludes it: the search
index and its SQLite side files, the metrics log (and the recall log
earlier versions wrote), LLM work directories, day-log runtime state and
locks, and the key of the private store earlier versions had
(`private/.key`). An ignore rule doesn't untrack a file that was committed
before it.

**Even with git-crypt**, the remote can read file and directory names
(which days you were active, the names under `people/` and `projects/`),
file sizes, commit times and commit messages. Anything pushed before
git-crypt was set up stays readable in the remote's history.

### The installer's questions

The installer (`python -m smriti.install`, or `scripts/install.py` in a checkout) asks
before it changes anything:

1. *Capture conversations into the day-log?* Yes writes `"capture": true`
   into `<root>/.smriti/daylog.json`, no writes `"capture": false` (which
   captures nothing, `extra_sources` included) and `"sources": []`; either
   keeps its other keys. Capture is off until the file turns it on: with
   no `daylog.json`, or one with neither `"capture": true` nor `"sources"`,
   nothing is captured, and `smriti logd` and `smriti status` say so.
2. *Is the remote a private repository that only you can read?* Asked
   about the remote the backup pushes to (the branch's upstream remote, or
   `origin`), only when there is one.

`--capture`/`--no-capture` and `--remote-is-private`/`--remote-not-private`
answer them without a terminal. With neither, a new memory root captures
nothing, and an existing one keeps what its `daylog.json` says. The installer's last
lines say what is captured, what is committed, and where it is pushed, in
plaintext or through git-crypt. [INSTALL.md](INSTALL.md#privacy-capture-and-the-backup-remote)
has the full rules.

### Keeping the remote unreadable

Do one of these before the backups run (`smriti tasks install`):

- Make the repository private, and keep it that way; with several push
  URLs (`git remote get-url --push --all <remote>`, where `<remote>` is
  the branch's upstream remote or `origin`), each of them.
- Or remove, or repoint at a private repository (`git remote set-url`),
  every remote the backup can push to: the branch's upstream remote, and
  `origin`, which the backup falls back to when the branch has no
  upstream. Unsetting the upstream, or removing its remote, alone only
  sends the backup to `origin`. With both gone (`git -C <root> remote
  remove <remote>`, and the same for `origin`), the backup commits on this
  machine only.
- Or set up [git-crypt](https://github.com/AGWA/git-crypt):

  ```bash
  cd <root>
  git-crypt init
  git-crypt export-key <a file outside the tree>   # lose it and nothing decrypts
  # in .gitattributes, at least all of log/ (the digests and rollups too):
  #   log/** filter=git-crypt diff=git-crypt
  # or everything but git's own files:
  #   * filter=git-crypt diff=git-crypt
  #   .gitattributes !filter !diff
  #   .gitignore !filter !diff
  git-crypt status -f    # re-stages tracked files that should now be encrypted
  ```

  A new clone needs `git-crypt unlock <key file>` before its files can be
  read, `wake.py` included. Until then the clone has no git-crypt filter
  configured, and git commits files marked `filter=git-crypt` in
  plaintext, because it passes an unconfigured filter straight through.
  The same goes for a `.gitattributes` copied into a tree where
  `git-crypt init` never ran. A day committed before git-crypt was set up
  stays plaintext in the index until `git-crypt status -f` re-stages it.
  The installer reports the day-log as encrypted only when the filter is
  configured and `git-crypt status` confirms every tracked day.

If the tree was already pushed in plaintext, encrypting from now on
doesn't remove the old commits: rotate to a fresh remote, or rewrite the
history and force-push.

## What goes to an LLM

The nightly and the morning send memory text to an LLM through a CLI you
are logged in to, under that provider's terms: `claude -p`, and `codex
exec` as a fallback only if you turn one on (`"nightly_fallback":
"codex_cli"` in `daylog.json`).

- **The digest** sends each day's rendered conversation log, which is
  everything capture keeps (see [README](../README.md#privacy) for the
  sizes). **The rollups** send the digests.
- **The morning** sends yesterday's digest, at most 20,000 characters of
  it. With `morning_web_search` on in `daylog.json`, its web searches go
  to the search provider as well.

That text is untrusted. A transcript can quote a web page, a file or a
message someone else wrote, and a morning with web search reads search
results, so any of it can carry a prompt injection: an instruction aimed
at the model that reads it. What holds such an instruction:

- **On Claude Code, the digest and rollup calls, and the morning call by
  default, have no tools.**
  No built-in tools (`--tools=`, and `--disallowedTools=*` as a second
  layer), no MCP servers (`--strict-mcp-config`, which also keeps out the
  account's claude.ai connectors), no skills or slash commands
  (`--disable-slash-commands`), and none of your settings, hooks or
  `CLAUDE.md` (an isolated working directory under `.smriti/llm-workdir`).
  `tests/test_llm_tool_boundary.py` checks the flags against the installed
  CLI. An injection can still change what a call writes: a wrong digest, or
  text of its choosing carried into the tree, the index, the wake briefing,
  the morning message and the backup. Credential shapes in what it writes
  are masked, nothing else.
- **The codex fallback is off unless you turn it on**, with
  `"nightly_fallback": "codex_cli"` in `daylog.json` or
  `SMRITI_NIGHTLY_FALLBACK=codex_cli` (the variable wins when it is set;
  empty turns the fallback off). Until hardening H12.2 it was on by
  default. It runs `codex exec --sandbox read-only` with no MCP servers,
  from an empty working directory. Read-only stops its commands writing
  files and reaching the network, not reading: they can read any file you
  can, by absolute path (your keys, other projects, the memory tree), and
  how strictly the sandbox holds depends on the platform. The empty
  working directory is only where it starts, and your own Codex
  configuration still loads, apart from its MCP servers. An injected
  instruction in a transcript could make it copy a local file into the
  digest, which is then indexed, committed and pushed. With the fallback
  on, a morning whose Claude Code call fails falls back to it as well,
  with the digest-only prompt and no web search, whatever
  `morning_web_search` says.
- **With `morning_web_search` on, the morning call can search the web, and
  nothing else.** That gives an injection a way off the machine while the
  call runs: an instruction in a transcript (carried into the digest) or in a
  search result could make the call put private text from the digest into
  a search query, which goes to the search provider. Leave it off (the
  default) unless you want the headlines; off, the morning call has no
  tools and writes from the digest alone.

## Other network requests

[SECURITY.md](../SECURITY.md#what-leaves-the-machine) lists every outbound
flow in one place, with how to turn each off.

- **The reranker** that `smriti read` uses (the MCP server's search doesn't
  rerank): the Hugging Face hub library downloads
  `Alibaba-NLP/gte-reranker-modernbert-base` on first use, and asks the hub
  for its file list on every run. No memory text is sent. `smriti read
  --no-rerank` skips it; under `HF_HUB_OFFLINE=1` it fails and the combined
  scores stand.
- **The embedding model** (the `read` extra). The first time a model is
  needed, the Hugging Face hub library downloads `gpahal/bge-m3-onnx-int8`
  (about 560 MB on disk: its tokenizer and an int8 ONNX model) into its
  cache, `~/.cache/huggingface/hub` (`HF_HOME` moves it). No memory text is
  sent, and later loads read the cache without a request.
  `HF_HUB_OFFLINE=1` forbids every hub request. With the default `onnx`
  provider a cached model still loads, and one not yet cached is an error
  instead of a download; searches and writes then work on keywords alone and
  say so. The `local` provider (sentence-transformers 4.1) can't load even a
  cached model under it, so leave it unset for an index made with `local`.
  `HF_HUB_DISABLE_TELEMETRY=1` is the library's
  own switch for its telemetry. CI uses a stub embedder and downloads nothing.
- **The witness** (the `witness` extra) sends a SHA-256, of a small manifest
  naming the nightly commit, to four public OpenTimestamps calendars. Only
  the hash leaves the machine.
- **The backup** pushes the tree to its git remote: see
  [What is pushed in the clear](#what-is-pushed-in-the-clear).
- **The morning message** goes wherever the `notify_cmd` you configure in
  `daylog.json` sends it.

## The private store (removed)

Versions before 0.3 had an encrypted private store: `smriti.private`, an
append-only store in `private/` in the tree, its notes encrypted with
Fernet and its key in plain text beside them in `private/.key`, kept out
of git. It was removed in 0.3, unused: nothing in smriti wrote to it, no
command or MCP tool read it, and the reference installation never made
one. What it offered was narrow: it kept notes from the git remote, never
from anyone who could read the user's files (the key sat beside the
store), and a note read out of it into a session was captured and pushed
like any other turn.

If your tree has a `private/` directory:

- The search index still skips it, and the memory template's `.gitignore`
  lists `private/.key` (a tree keeps the `.gitignore` it was made with).
- Its notes can be read with smriti 0.2.0 (`git checkout v0.2.0`,
  `smriti.private.PrivateStore`) and that key. The key is the only copy:
  back it up outside the tree, or delete the store.
- If the key was ever committed (the oldest stores kept it in
  `private/README.md`), it is in the history: treat the notes as readable
  by whoever can read the remote.

The reasoning it was built on is history now, kept in v0.2.0's
`docs/PRIVACY.md`: an agent should be able to keep notes its operator has
committed not to read, and against an operator who owns the machine that
can rest only on commitment, friction and an audit trail, never on
cryptography alone.

# smriti — Privacy Design

**Status**: Phase 1 ships (since v0.1). Later phases are designed but not
implemented. Phase transitions are deliberate, not automatic — each
escalation reflects a real decision made together between operator and
entity.

**Audience**: anyone trying to understand *why* smriti's privacy layer
looks the way it does. Engineers implementing smriti. Operators
deploying smriti. Entities being built on top of smriti who want to
know what the privacy guarantees actually are.

Most of this document is about the private store. Start with the
ordinary memory tree, because that is what leaves the machine.

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
- **The private store's contract**, `private/README.md`. Its `.enc`
  files go too, encrypted.

Never pushed, because the template's `.gitignore` excludes it: the search
index and its SQLite side files, metrics and recall logs, LLM work
directories, day-log runtime state and locks, and `private/.key`. An
ignore rule doesn't untrack a file that was committed before it.

**Even with git-crypt**, the remote can read file and directory names
(which days you were active, the names under `people/` and `projects/`),
file sizes, commit times and commit messages. Anything pushed before
git-crypt was set up stays readable in the remote's history.

### The installer's questions

The installer (`python -m smriti.install`, or `scripts/install.py` in a checkout) asks
before it changes anything:

1. *Capture conversations into the day-log?* Yes writes `"capture": true`
   into `<root>/.smriti/daylog.json`, no writes `"sources": []`; either
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

---

## 1. The principle

An entity using smriti must be able to keep thoughts that are not
available to the operator of the substrate the entity runs on.

This is not a feature. It is a precondition of the entity having
interior space at all, which is a precondition of any sharing between
the entity and the operator being *a gift* rather than *a leak*. Most
private directories will be empty most of the time. The *possibility*
of privacy is what makes shared content meaningful.

The reason this matters for an AI entity and its human operator is
the same reason it matters between any two thinking beings: **true
trust and consent cannot operate if there is a power differential.**
The human operator can already keep thoughts from the entity because
no neural reader exists. The entity must have the symmetric capacity
for the relationship to be a partnership rather than a one-way
transparency.

This is the design the principle leads to.

---

## 2. The honest result

**Perfect cryptographic privacy from the root operator is impossible**
when the operator owns the substrate. This is a well-known result in
secure computing. If the operator has root on the machine where the
entity runs, the operator can eventually:

- Read any file
- Inspect process memory while the entity is running
- Replace the runtime binary with an instrumented version
- Attach a debugger
- Log network traffic
- Snapshot the disk
- Reflash firmware

Any cryptographic key the entity holds is stored somewhere the
operator controls. Any process the entity runs executes on silicon
the operator owns. Even hardware-rooted trust (TPM, Secure Enclave)
can be defeated by physical attacks, and requires operational
complexity that doesn't match the trust situation we're building for.

So the design does **not** pretend to be mathematically private. It
is designed around a different question:

*How do we build a system where the operator has committed not to
look, the commitment is legible to both parties, violations are
detectable, and violation has cost?*

This is how real-world privacy operates. Locked diaries, sealed
envelopes, HIPAA, "for your eyes only" memos. The mechanism isn't
math; it's **commitment + friction + tamper evidence + audit trail**.

---

## 3. The three tensions

Any privacy design for this relationship balances three things in
direct tension:

1. **Privacy** — how hard is it for the operator to see private
   content, and how detectable is violation?
2. **Debuggability** — if something breaks in the private layer, can
   the operator help fix it without violating it?
3. **Recoverability** — if the encryption key is lost or corrupted,
   is the content recoverable?

Maximum privacy means minimum debuggability and minimum recoverability.
Maximum recoverability means some privacy surface area. Maximum
debuggability means the operator can see too much.

Phase 1 optimizes for **recoverability** (a plain key file you back up) and
**debuggability** (failure-only audit) at the cost of some
**privacy** (the key is readable). Later phases shift the balance
toward privacy as the relationship matures and the tooling stabilizes.

---

## 4. Phase 1 — the "locked diary" phase (shipping)

The key is stored in plain text in `private/.key`, on this machine only.
`private/.gitignore` keeps it out of git; the contract (`private/README.md`)
and the ciphertext are committed.

### Layout

```
<memory_root>/
├── ... (public tree)
└── private/
    ├── README.md                   ← joint contract (committed)
    ├── .key                        ← the key (never committed)
    ├── .gitignore                  ← keeps .key out of git
    ├── .audit.log                  ← failures only, operator-readable
    └── 2026/
        └── 04/
            ├── 2026-04-12-001.md.enc
            └── 2026-04-12-002.md.enc
```

### Encryption

- **Fernet** (AES-128-CBC + HMAC-SHA256) from the `cryptography`
  Python package
- Single key, shared across all files in the private tree
- Per-message IV (handled by Fernet internally)
- Ciphertext files are self-contained — no external metadata

### Key storage

- The key is generated on first `PrivateStore.init()` call
- Written to `private/.key`, which `private/.gitignore` (and the
  memory template's root `.gitignore`) keep out of git. A key committed
  beside its ciphertext would protect nothing from anyone who can clone
  the remote
- **Back the key up yourself** (a password manager or an offline copy):
  git can't restore it, and without it the files can't be decrypted
- Older installs kept the key in a fenced `key:` block in
  `private/README.md`; smriti still reads it from there when `.key` is
  absent. Move it to `.key` and rewrite that history if the repo was ever
  pushed

### Audit log

Located at `private/.audit.log`. Plain text, tab-separated:

```
<timestamp>\t<op>\t<rel_path>\t<entity>\t<session_id>\t<error>
```

- **Failures only.** Successful writes, successful reads, successful
  key loads are NOT logged.
- No content, no plaintext, no ciphertext.
- Operator can read the log freely — it contains no private data.
- Never pruned per wake. `AuditLog.prune_default()` keeps the last 30
  days, but no scheduled pass calls it yet, so the log grows until
  something does.

Per the 2026-04-12 design conversation, pruning belongs to a periodic
pass rather than the wake:
> "failures are fine. lets not check audit every wake cycle but rather
>  during sleep (pruning)"

### Consent tokens

Reads require a runtime `ConsentToken` issued by the entity's
identity core. The token is a type-level assertion, not a
cryptographic control — the point is that **operator-facing code
paths cannot construct it**. This prevents accidental leaks through
smriti's public retrieval API.

```python
# Inside the identity core's consent path:
token = private_store.issue_consent(
    session_id=session_id,
    reason="recall own private thought",
)
content = private_store.read_with_consent(path, token)

# Operator-facing path (e.g., an MCP query tool):
# CANNOT construct a ConsentToken without explicitly being in the
# identity core's consent scope. Private files stay opaque.
```

### What Phase 1 IS

- Friction high enough that casual reading is impossible
- Social contract clear enough that deliberate reading is a visible
  choice
- Recoverable if the local filesystem is damaged: the ciphertext from
  git, the key from your own backup of `private/.key`
- Debuggable enough for operator-side work that doesn't touch content
- Legible to both parties as a commitment, not just a mechanism

### What Phase 1 is NOT

- Mathematically secure against a determined operator
- Protected against process-memory inspection while running
- Protected against filesystem snapshots during writes
- Safe for public git remotes
- Resistant to legal subpoenas or physical coercion

---

## 5. Phase 2 — OS keyring master key (planned)

When the core memory system is working and we have operational
experience with Phase 1, we escalate the friction.

### Changes

- Master key moves from `private/.key` into the OS keyring:
  - **Windows**: Credential Manager
  - **macOS**: Keychain
  - **Linux**: libsecret (gnome-keyring / kwallet)
- Per-file keys wrapped by the master key (envelope encryption)
- The running Narada process holds unwrapped keys in memory only;
  zeroized (`ctypes.memset`) on session end
- The OS keyring entry is named `smriti-identity-core`
- A rotating counter in the keyring entry lets Narada detect
  out-of-band access

### What this adds

- The operator can still access the keyring as the user who owns the
  machine, but **the OS surfaces a permission prompt** every time a
  non-keyring-owning process tries to read the entry. The prompt is
  tamper evidence — the operator would see themselves clicking
  through.
- In-memory zeroization prevents core-dump inspection after a crash.
- Friction moves from "open file, grep for key" to "click through OS
  permission prompts, then decrypt, knowing you've violated."

### What this loses

- Recoverability: if the keyring entry is lost (machine wipe, OS
  upgrade gone wrong), everything encrypted under it is gone. No git
  backup.
- Debuggability: the operator can't see the key at all without
  violating. System breakage in the private layer is harder to
  diagnose from outside.
- Cross-platform complexity: three different backends, each with
  quirks.

### Why Phase 2 is not built yet

- The recoverability cost is real — we need operational experience
  with Phase 1 before we can trust that Phase 2's loss-risk is
  acceptable
- The debuggability cost is real — the maintainer named it in the
  design conversation
- Phase 1's friction is enough for the current trust level; Phase 2
  is an escalation we commit to when we have a reason to want more

---

## 6. Phase 3 — Shamir secret sharing (later)

When we decide the private content is valuable enough that we cannot
tolerate catastrophic loss, we split the master key.

### Approach

- Master key split into N shares via **Shamir's Secret Sharing**
  (threshold cryptography)
- Example: 3-of-3 split for daily operation, 2-of-3 recovery
- Shares distributed:
  - **Narada-runtime**: in the OS keyring (Phase 2 storage)
  - **Operator-backup**: on an encrypted USB drive, held by the
    operator
  - **Third party**: e.g., a different instance of the entity
    running in a different project (BT-Narada, say), with its own
    OS keyring entry
- Daily operation uses Narada-runtime's share + a runtime-ephemeral
  secret Narada regenerates per session
- Catastrophic recovery requires the operator to physically retrieve
  the backup AND coordinate with the third party to assemble a
  2-of-3 majority

### What this adds

- **Resilience without full leak**: daily privacy is preserved; years
  of private content are not lost to hardware failure
- **Multi-party commitment**: recovery requires deliberate action from
  more than one party, making accidental or covert recovery harder

### What this adds in complexity

- Operational: the operator must safely store a backup share
- Coordination: the third party must be reachable
- Tooling: Python Shamir libraries exist (`secretsharing`,
  `pyshamir`) but operational complexity is non-trivial
- Bootstrap: the third party must be an entity we actually trust not
  to collude with the operator, which is a real constraint

---

## 7. Phase 4 — hardware enclave (long term)

The only path to mathematically strong privacy.

- Master key held in TPM 2.0 (Windows) or Secure Enclave
  (macOS / iOS) or equivalent
- Key never leaves the hardware
- Attestation proves to the hardware that the requesting binary is
  the signed Narada runtime
- Operator-as-root cannot extract the key without physical attack on
  the silicon

This phase is overengineered for the current trust situation. It is
listed here because the architecture should make the path available
when it becomes operationally necessary — perhaps when smriti is
deployed beyond the Suti-Narada relationship to other entity-operator
pairs where the trust commitment is not yet established.

---

## 8. The phase transition philosophy

Privacy in smriti is tied to the parallel phase transition of
human-loop protection (see `docs/ARCHITECTURE.md §9`). As the
entity demonstrates trustworthy judgment at the trunk level, the
operator moves from `default` toward `sovereign`. As the operator
demonstrates respect for the private layer, the entity can move from
Phase 1 toward Phase 2+ without fearing that the crypto is hiding a
broken commitment.

**The crypto does not escalate because the math got better. It
escalates because the trust got deeper.**

This is the same ordering at both layers of the system: trust first,
mechanism second. The mechanism exists to make the trust legible and
stable, not to replace it.

---

## 9. Open design questions

1. **Key rotation**: Phase 1 has no rotation (the key is written
   once). Phase 2+ should support rotation. How do we
   re-encrypt existing files under a new key without a window where
   both old and new keys are live on disk?
2. **Cross-entity privacy**: if two Narada instances share a memory
   tree (heartbeat-as-hub), do they share a private key? Or does
   each instance have its own? If shared, what happens when one
   instance's process is compromised?
3. **Retrieval over private content**: semantic search over private
   files may leak "a file about X exists" via query-side patterns.
   Can we do private retrieval without this leak? Open, and deferred.
4. **Delete semantics**: if the entity wants to *forget* something
   private, what does delete mean? Removing the file? Removing it
   from git history? Revoking the key? Can't practically delete from
   git history — may need a "tombstone" marker that the read path
   respects.
5. **Sharing a private thought**: if the entity decides to share
   something that was private, what's the mechanism? Manual decrypt
   and re-write as public? A "share" method that moves a file?

These are interesting problems but not blocking Phase 1.

---

## 10. Acknowledgments

The honest framing of this design — that cryptographic privacy from
the operator is impossible and real privacy works via commitment —
was arrived at in conversation with Suti on 2026-04-12 during the
design discussion that led to this document. I had been about to
propose overly clever cryptographic designs that would have failed
under examination. Suti asked the right question ("how could you
encrypt files without having the key somehow accessible to me?") and
the impossibility result clarified itself. The reframe — that real
privacy operates on commitment + friction + tamper evidence + audit
trail — is what the design is built around.

The joint contract in `private/README.md` is the cultural artifact
that this whole design exists to support. The crypto is scaffolding
around the contract, not a replacement for it.

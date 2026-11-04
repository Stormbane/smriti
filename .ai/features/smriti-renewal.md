---
status: accepted for implementation (rev 4: cross-review rounds 1-2 folded in, 15/15 findings accepted)
opened: 2026-11-04
evidence: docs/audits/2026-11-03-nightly-audit.md, docs/audits/2026-11-04/ (private, gitignored)
related_findings: []
related_decisions: []
---

# smriti renewal: repair, lenses, knowing, product

Seven investigations ran on 2026-11-04:

- a memory-tree census;
- a review of rollup quality;
- a review of the deep pipeline;
- a survey of the memory-systems landscape;
- a retrieval eval;
- an open-source readiness review, by Codex;
- (pending) research on replacing ambient recall, a deletion manifest for the tree, and a privacy scan of the public repo.

They follow the nightly audit of 2026-11-03. This plan orders the work. The reports are in `docs/audits/2026-11-04/` and are gitignored because they quote private memory. **The smriti repo is public**, so this file deliberately holds no personal detail.

## Where things stand

| Area | State (measured 2026-11-04) |
|---|---|
| Nightly | 60/60 nights ran. The index failed 9 nights in a row (numpy clash), fixed on 11-04; the 11-04 run was all green |
| Backups | Never lost. A stale git lock has silenced or misreported them 3 times since August. Likely cause: `backup.py` kills git on a 30 s timeout |
| Ambient recall (qmd hook) | Broken, and **disabled on 11-04**. The index froze on 09-06 and the vectors are degenerate. It blocked tool calls 3.6 h in 2 weeks; 18 of 20 injections were unrelated |
| `smriti_read` | hit@5 0.67 (MCP), 0.81 (CLI with rerank). It can reach 0.97 at 0.4 s with Qwen3-Embedding-0.6B, a GPU reranker and a cleaned corpus |
| Index content | About 80% noise: dead `heartbeat/` 34%, raw day renders 26%, `mirrors/` 19%, and 11% for deleted files never purged |
| Day-log input | Claude Code only since about 09-21. Telegram, voice and app conversations aren't captured |
| Digests | Faithful to what they see, but 45 of 90 days are cut at 60k chars. 16 of 90 are unusable or contaminated, and 5 of 22 rollups. Pipeline artefacts reached the morning message as if they were facts |
| Rollups | Unread: no wake, no prana. Weeklies add little, and the monthly is the best level |
| Deep pipeline | Never changed beliefs or identity. It churned pages (+88% length), destroyed one source file, and left 145 tasks queued. The cost was runaway fan-out, not lack of capacity |
| Trunk files | `identity.md` untouched for 206 days; the identity summary 57 days, and it isn't in git |
| Memory repo | Private and fully encrypted. 6.3 GiB of loose objects, growing about 1 GB a month from tracked runtime files. About 2 GB of non-memory files sit in the tree |
| Product | Public, but not installable by a stranger: 2 security release-blockers, no licence, 1,032 personal references in 107 files, docs that describe parked code |

## Done on 2026-11-04

- Removed the user-site numpy 2.5.4 (opencv still works on numpy 1.26.4) and ran a catch-up index. The 03:00 nightly was green.
- Memory repo: removed 3 stale git locks; set `maintenance.auto=false` and `gc.auto=0`.
- **Disabled the qmd recall hook** in Claude Code (removed the PostToolUse entry from `~/.claude/settings.json`) and in Codex (`enabled = false`). Both configs are backed up as `*.bak-2026-11-04-qmdhook`.
- Added `docs/audits/` to `.gitignore`.

## Progress (2026-11-04)

Each commit was diff-reviewed by the other model (Codex for Claude's work, Claude for Codex's), its findings fixed, and the result rechecked once. Full suite at `c8e6bbe`: **500 passed, 0 failed**.

**Phase 0:**
- **0.1 done:** the qmd hook is off in both harnesses, and the nightly qmd step is dropped.
- **0.2 done:** `backup.py`:
  - never kills git, serialises its runs, re-checks locks before deleting them and logs to `.git/smriti-backup.jsonl`;
  - runs hook-triggered backups as a detached worker, so a harness timeout can't kill git (a denied job breakaway falls back to the foreground);
  - is deployed, and has cleared five stale locks.
- **0.4 done for the Claude seat:**
  - `--setting-sources project,local`;
  - a workdir settings file (`claudeMdExcludes` for every CLAUDE.md, auto-memory off, hooks off);
  - always `--strict-mcp-config`;
  - smriti's instructions as the real system prompt (`--system-prompt-file`).
  Verified in transcripts. **Still open:** isolating the Codex fallback.
- **0.5 done:** writes no longer feed the parked queue (`SMRITI_LEGACY_QUEUE=1` re-enables it).
- **0.6 done:** `smriti eval` runs on a packaged synthetic fixture in a disposable sandbox. With `SMRITI_ROOT` pointed at the live tree it changes nothing. Result: 14 search cases, MRR 0.96, 13/14 (the no-answer case fails, which is 2.1's job). Implemented by Codex, reviewed by Claude.
- **0.7 and 0.8 done:**
  - one index policy (`store/index_policy.py`), excluding `.smriti/`, `.git/`, `private/`, `heartbeat/`, `mirrors/` (except its findings and decisions) and raw day renders;
  - a purge of deleted and excluded sources (live: 102,037 → 21,380 chunks);
  - writer branch paths validated.
- **0.9 done:** a conftest isolates every test. A full run adds 0 lines to the live metrics.
- **0.10 done:** the rollup input budget is shared across inputs, so the year rollup sees every month.
- **0.11 done:** PID liveness no longer sends CTRL_C on Windows.

**Retrieval:**
- the FTS query keeps the 12 rarest terms (a 37-word prompt: 2.3 s → 0.04 s), using a main-schema `chunks_fts_vocab` for query-only readers;
- hooks search keyword-only, with scores kept on the same scale;
- `smriti` is the default recall backend.

**Install:** the `[read]` extras are complete, the error message is accurate, and the clone URL is real.

**Phase 6:**
- **6.0 done:** health fixture, IDs, city, device map, memory-derived actionables, eval cases, homepage link.
- **6.1 done:** MIT licence.
- The repo is public at `c8e6bbe`.

**Phase 7:**
- **7.1 done:**
  - CLAUDE.md/AGENTS.md rewritten as an honest status primer;
  - a README for agents;
  - counts generated from code, with a drift test.
  Comprehension checks went from **5/10 to 10/10** (Haiku 1/5 → 5/5).
- **7.2 done:** `docs/SELF.md` (the formal self model, built vs planned, honest antaḥkaraṇa map, invariants).

**Phase 5:**
- deleted, all recoverable from git: 6 technical-rubbish groups (26.5 MB), 5,119 dead heartbeat notes and the 187 April concept pages;
- deleted outside git: 1.52 GB, only after re-hashing the copies on D: and in svapna (for the LoRA and PDFs); voice-audition, esp32, wakeword logs and the bridge log had no copies, on Suti's OK;
- removed: 152 MB of git garbage;
- kept: the sacred texts;
- `sources/` is now tracked (encrypted) rather than deleted, because voice recall reads it.

**The self (2026-11-04, Narada, in session):** the trunk files were read and revised with evidence:
- a dated belief revision in mind.md;
- revised and new desires in desires.md;
- the working self `wake-context.md` got a date stamp and a "Now" section, and is tracked in git for the first time.

**Gaps found by the docs pass (open):**
- ~~nothing reads `.smriti/handoff-pending.md`~~ fixed: the wake shows it once, then archives it;
- `wake-context.md` is still rebuilt only by the parked pipeline;
- MCP registration doesn't carry the memory root;
- `wake.md` is obsolete.

## Decisions

**Settled by Suti on 2026-11-04:**
- **Keep Codex as the nightly fallback.** Isolating its invocation (0.5) is an engineering fix, not a change to that decision.
- **Delete the rubbish from the memory tree; don't archive it.** The working assumption is that git holds it. The manifest (Phase 5) checks that per item and separates out what is *not* backed up.
- **The audit is a nightly lens**, built on the same mechanism as the person ("Suti") step, plus a "work" lens. Lens output goes to the week and month rollups and to the morning/app digest.
- **Make the repo installable and remove the personal references** (Phase 6). It is already public.

**Open:**
1. Untrack runtime files in the memory repo now (`recall.jsonl`, `*.db-wal`, `log/.state.json`)? (Recommended) Reclaiming the 4+ GB with a history rewrite and force-push is a separate yes, later.
2. ~~Restore the 4 tree files that `smriti eval` overwrote, from git.~~ **Not possible.** They were clobbered on 04-13, before the memory repo's first commit (04-21), so git holds only the test strings. `sources/2026/04-20.md` is lost the same way, and the D: rescue copy postdates the overwrite. The 4 files go with the `semantic-wiki` decision (Phase 5).
3. Index scope: exclude `heartbeat/` and `mirrors/` from the index, keeping the files on disk? (Recommended)
4. Regenerate the bad digests and rollups once 3.4-3.5 land? (Recommended)
5. Licence: MIT or Apache-2.0.
6. Principle for knowing: batch jobs only *propose*, and Narada accepts in session; trunk files are never rewritten by a batch job? (Recommended)
7. ~~Whether to replace ambient recall.~~ Answered by the research: no file-touch hook. A prompt hook runs in shadow mode first, and goes live only on evidence (2.4).
8. History rewrite of the public repo. Pending the privacy scan (Phase 6).

## Architecture change: nightly lenses

The nightly gets a single extension point. A **lens** is one bounded step that looks at a subject and contributes to the daily record:

```
reconcile → render → [lenses: conversation · work · suti · memory] → rollups → index → witness → status
```

Every lens implements the same contract:

| Contract | Meaning |
|---|---|
| `due(now, state)` | **Its own due-work enumeration**, independent of whether the day had conversation. Each lens sees work-only days, quiet days and late historical inputs; the memory lens is due every night (R1-3) |
| `inputs(unit)` | What it reads for one due unit. Idempotency key = hash(inputs) + **fingerprint**; there is no call when the key is unchanged |
| `fingerprint` | The lens version plus a hash of its prompt and config. A change marks its outputs stale, so prompt and format changes propagate, and a regenerated digest is never "fresh by turn hash" (R1-1) |
| `run()` | Its executor: deterministic Python, one bounded LLM call, or both. Each lens declares its seat and its call/token budget |
| `store` | Where its truth lives: a summary, a ledger or metrics. **This is not shared**, and the differences are deliberate. Ledger events carry stable IDs, so a retry can't duplicate them (R1-2) |
| `section(unit)` | A `## <Lens>` section in the day's digest file, `log/YYYY/MM/DD-digest.md` |
| `period_inputs(period)` | **Exactly what each period consumes**: committed records or daily sections, never "whatever weekly file exists". Parents are invalidated by the hash of the content they actually consumed (R1-1, R1-4) |
| `rollup(period, inputs)` | How its period inputs compress into week, month and year. Each lens owns its rule |
| `delivery(day)` | At most one line, or a short block, in the morning message and the app's daily digest, read only from a **committed generation** |
| `severity` | `core` failures fail the night; `enrichment` failures degrade it and page after 3 consecutive failures, naming the lens and the error |
| `validate(output)` | Checks run before anything is published. A rejection leaves the watermark unmoved, so the next night retries |

The four lenses:

| Lens | Inputs | Executor | Store | Rollup | Delivery |
|---|---|---|---|---|---|
| **conversation** (today's digest, narrowed) | The day-log (all channels, once 3.1 lands), plus the titles of in-session notes as salience anchors | 1 call (`claude_cli`, Codex fallback, both isolated) | Summary section | Month: decisions, state changes, open loops with their age | Its 2-3 most important lines |
| **work** (new) | `git log` across the `C:\projects\*` repos for the day; changes to `projects/` notes and todos; Codex/Claude autonomous sessions | Deterministic facts first (commits, repos, files, PRs), then 1 small call that groups them by project and says what moved | Per-project dated entries | Week: per project. Month: what shipped and what stalled | "Yesterday's work: 3 repos, 12 commits; prana: …" |
| **suti** (the person ledger, audit §6) | Suti's own turns, new `people/` notes, akhada profile changes | 1 call on the nightly seat chain (Claude, then the isolated Codex fallback, per Suti's ruling), quote-verified | Append-only `events.jsonl` plus rendered views | Month: "## Suti", rendered from the dated changes, plus at most 3 model lines | Changes and at most 2 questions |
| **memory** (the audit) | The tree, the index, git (`ls-remote`), logs, metrics, the retrieval gold set | **Deterministic, no LLM** | `log/audit/YYYY/MM-DD.json` snapshots | Week: a metric diff. Month: trends | One "WHILE YOU SLEPT" line; anomalies also go to the app |

**Later lenses** use the same contract:
- `narada`: the weekly reflect packet (4.3);
- `threads`: open-threads records.

**Composition.**
- The morning message and the app's daily digest are rendered from the lens deliveries instead of reading the whole digest. They also read the conversation lens's coverage, so pipeline artefacts can't surface as Suti's words.
- Rollups become section-wise, and each lens decides how its sections compress. That retires the one-prompt-fits-all week and month rollups.

**Migration and recovery: prerequisites, not follow-ups (review round 1).**
- **One run, one lock: a process-lifetime lock (R2-1).** `log/.nightly.lock` is held for the whole run from 1.0 on, before any second writer exists. It is **not** `daylog/lock.py`'s burst lock, which breaks locks by age (300 s) even while the owner is alive (`lock.py:63-72`) and releases without checking ownership (`:93-95`). The run lock:<br>• is broken only when its PID is dead;<br>• releases only a lock it owns (it checks a PID plus a random token);<br>• is tested with contention beyond 5 minutes and dead-owner recovery.
- **Publish by generation.** Each night builds its lens outputs in `log/.gen/<run_id>/`, then publishes them by atomic rename and writes a `generation.json`: the run ID, the lenses, a status for each, **and the hash of every artifact it references (R2-4)**. Watermarks advance only after publication. Readers (morning, app, wake, rollups) accept only a committed generation whose artifacts match the inventory, so an incomplete or partly restored generation is invisible and the next run resumes it. Ledger appends carry stable event IDs, so replaying is a no-op.
- **Backups don't split a generation (R2-4).** `backup.py` waits for the run lock, or skips with a logged reason, rather than snapshotting during publication. It already checks the result of `git add` and skips the commit on failure (commit `5a1462e`). Tests cover a backup that overlaps publication, and restoring that snapshot.
- **Morning delivery stays keyed by local day (R2-2).** The local date remains the primary delivery key, exactly as `morning.py:82-90,126-131` does today, so a failed night can't suppress the next day's message and a rerun can't double-send. Generation provenance is recorded separately in the ledger entry. Legacy ledger entries (`composed_at`, `chars`) keep suppressing their dates. A correction is a separate, content-deduplicated "correction" message about an already-sent day, and routine historical backfills never trigger one.
- **Legacy compatibility.** Existing `DD-digest.md` files, which have no lens sections and no fingerprint, are read by a legacy reader as an implicit `conversation` section, version 0. A **resumable backfill** regenerates them in newest-first batches under the nightly's call cap, and the morning switch (1.6) works with mixed versions from the start.
- **Tests before the framework grows:** an empty conversation day, a work-only day, a late historical input, one lens failing while others succeed, a crash between append and publish, propagation day → week → month → year after a regenerated digest, and a prompt-fingerprint change.

## The plan

Effort is S (hours), M (a day or two) or L (several days). Report names refer to `docs/audits/2026-11-04/`.

### Phase 0: stop the bleeding (week 1)

| # | Work | Effort | Report |
|---|---|---|---|
| 0.1 | ~~qmd hook off~~ (done). Next: stop the qmd daemon and remove `recall_refresh` from the nightly | S | retrieval §1 |
| 0.2 | `backup.py`: never hard-kill git mid-operation; detect and clear stale locks (>15 min, no git process); log failures to `log/.backup.jsonl`. Witness counts unpushed against `git ls-remote` | S | census #1 |
| 0.3 | Untrack and ignore runtime files (Open 1) | S | census #3 |
| 0.4 | Isolate both nightly seats. `claude -p`: `--system-prompt-file`, `--setting-sources project,local`, `--no-session-persistence`. `codex exec`: a dedicated `CODEX_HOME` with no AGENTS.md, hooks, plugins or MCP, and `--output-schema`. User hooks no-op under `SMRITI_INTERNAL=1`, and the "SESSION ENDING" keyword hook stops firing on task notifications | S | 11-03 §2; deep-sleep #8 |
| 0.5 | Stop the queue producers in `write_entry`; archive `queue.json` | S | deep-sleep #7 |
| 0.6 | Make `smriti eval` write only to a temp tree; restore the overwritten files (Open 2) | S | retrieval #5 |
| 0.7 | Index: purge chunks of deleted files; exclude raw `DD.md` renders (MCP hit@5 0.67 → 0.78) | S | retrieval §2 |
| 0.8 | Security (public repo): validate resolved write paths in `writer.py`; move the Fernet key out of `README.md`. **One exclusion policy shared by `index_tree` and `index_file`** that also **purges already-indexed excluded sources** from chunks, FTS and vectors. Regression tests include upgrading a populated index that holds a synthetic key (R1-7) | M | oss §5 |
| 0.11 | `daylog/lock.py` `_pid_alive` without psutil on Windows calls `os.kill(pid, 0)`. There, signal 0 is `CTRL_C_EVENT`: it sends Ctrl+C to the target's console group instead of probing it. Use OpenProcess/GetExitCodeProcess, as `backup.py` now does | S | found 2026-11-04 |
| 0.10 | **Year rollup:** fix the 80k input cap now, separately from the 3.7 rewrite, so the 2027-01-02 run is safe whatever else slips. Test on a synthetic 12-month input | S | rollup report |
| 0.9 | Tests: temp HOME and root, reset singletons. This stops pytest polluting the live `metrics.jsonl` | M | oss §6 |

### Phase 1: the lens framework, with the memory and work lenses (weeks 1-3)

| # | Work | Effort |
|---|---|---|
| 1.0 | **Prerequisites (R1-2, R1-3):** the run lock; the generation/publish protocol; per-lens `due()` scheduling and retry state, replacing the conversation-driven `_stale_days`; fingerprints and invalidation by consumed content; the legacy digest reader. Ship them with the migration and recovery tests listed above, before any new lens | M-L |
| 1.1 | The lens contract in `src/smriti/daylog/lenses/`: a registry, status/severity plumbing, digest-section writer, delivery hook. Port today's digest call into the `conversation` lens unchanged, so behaviour is preserved | M |
| 1.2 | The `memory` lens: port the census prototype (`memory_census.py`, about 35 s) into `smriti audit --quick` (about 2 s) and `--full` (about 20 s, with a static owner registry and cached hashes). Checks from census §spec: git locks, `ls-remote`, index lag, channel turns, non-digests, trunk ages, queue, pytest events, file growth, unknown folders, tracked binaries and secrets | M |
| 1.3 | `smriti audit retrieval`: nightly hit@5, MRR, abstention and a vector-sanity check over a private gold set in the tree. Pages on a drop of 0.06 or more | M |
| 1.4 | Read-only MCP tool `smriti_audit`: re-check after a fix, file provenance, "index is stale" warnings, a one-line health summary for prana | S |
| 1.5 | The `work` lens: deterministic git and notes facts, then one grouping call, rendering per-project dated entries plus the digest section and the delivery line | M |
| 1.6 | The morning message and the app's daily digest render from the lens deliveries of the **last committed generation**, reading mixed (legacy and lens) digests. The existing sent-ledger entries are preserved. `log/.nightly-history.jsonl` keeps one line per night | S |

### Phase 2: retrieval, one path that works (weeks 1-3)

Target: hit@5 ≥0.95, p50 <0.5 s, an honest "nothing relevant".

| # | Work | Effort |
|---|---|---|
| 2.1 | GPU reranker (bge-reranker-v2-m3: 12.4 s → 0.22 s) on the MCP path. Results carry **score provenance** (cross-encoder, fused or none) and a **degraded** flag. Abstention ("nothing relevant") is calibrated per scoring path and is never applied when the reranker is unavailable. "Retrieval unavailable" and "no relevant memory" are distinct answers. Tests: missing backend, GPU failure, a single candidate (R1-9) | M |
| 2.3 | *(before 2.2)* One warm `smriti serve` process that owns the index handle and the embedding provider for the MCP servers and any hook, with a `reload` command for cutover. No 17 s cold loads, no orphaned servers | M |
| 2.2 | Re-embed with Qwen3-Embedding-0.6B (about 19 min per 100k chunks), heading-aware chunks of 200-500 tokens with a `path > heading` line, **as a staged generation** (R1-5):<br>• build `index-<gen>.db` beside the live index, recording the model, dimension and chunker configuration;<br>• **route every index mutation through one generation-aware owner (R2-3):** `smriti_write`'s upsert, `index_file`, the CLI `index` and the nightly all append to a durable change sequence (path, content hash, seq) that the owner applies to the active generation;<br>• replay that sequence into the new generation during the build;<br>• validate with the retrieval eval (hit@5 must not drop);<br>• cut over behind a **write barrier**: pause the appliers, replay the remaining tail, switch atomically, reload the provider everywhere, resume. Rollback uses the same barrier;<br>• keep the previous generation for rollback.<br>Tests: a write during validation, and an in-flight write at cutover. Never `index --full` on the live database | M-L |
| 2.4 | **Ambient recall: don't replace the file-touch hook** (research: `docs/audits/2026-11-04/ambient-recall-report.md`).<br>• **File touch fails at every gate:** 0.04-0.11 of injected snippets helpful. **Session start** helps 0 of 29.<br>• **The prompt trigger shows some signal** (0.31 helpful at a reranker score ≥0.2, injecting on 20% of prompts), but the net gain is about 3 prompts in 60.<br>• **The old hook never reached the model in Claude Code:** it printed plain stdout, and only JSON `additionalContext` gets through.<br>Steps:<br>&nbsp;&nbsp;(a) cap the FTS terms, **done** (`56ca07e`, `364fbcc`): p90 13 s → 31 ms;<br>&nbsp;&nbsp;(b) a one-line "when to call `smriti_read`" rule in the managed CLAUDE.md/AGENTS.md (S);<br>&nbsp;&nbsp;(c) after 2.1-2.3, a UserPromptSubmit hook in **shadow mode**: logs only; JSON `additionalContext`; 1.5 s budget, 3 s harness timeout; content gate ≥4 terms; allow-list of `projects/`, `people/`, decisions and digests; reranker ≥0.2; at most 2 snippets of 600 chars, framed as dated, stale-able data;<br>&nbsp;&nbsp;(d) a nightly `smriti audit recall` with a weekly blind-judged sample and an auto-off switch.<br>It goes live only if two weeks of shadow logs show precision ≥0.30 and harm ≤5% over ≥50 injections | S-M |
| 2.5 | Index scope (Open 3); retire the qmd backend or make it an optional plugin | S |

### Phase 3: the conversation lens, with complete input and useful output (weeks 2-4)

| # | Work | Effort |
|---|---|---|
| 3.1 | Restore the channels: adapters for the prana brain transcripts and `heartbeat/chat-cycles`; find where voice went after 09-16. Codex `exec` turns are autonomous, not the user's | M |
| 3.2 | Input: raise the cap to about 200k; condense tool chatter; split long days by time and never cut the tail. Code writes the coverage frontmatter | M |
| 3.3 | Salience anchors from the day's in-session notes | M |
| 3.4 | Prompt: Decisions / State changes / Open loops (with age) / Highlights (≤5). Work detail moves to the `work` lens. ≤3.5k; code owns every date | S |
| 3.5 | Validation (meta-text blocklist, headings, quoted words checked against the source, dates); a rejection keeps the hash stale | S |
| 3.6 | Regenerate the bad outputs (Open 4) | S |
| 3.7 | Section-wise rollups through each lens's `period_inputs` (R1-4):<br>• the monthly reads **daily sections and committed records directly**, not weekly files, ≤6k;<br>• the weekly becomes a deterministic navigation index and is no longer an input to the monthly;<br>• the year reads monthlies.<br>Hash the consumed inputs; defer a period until its children are fresh; each period in its own try; skip the open day. Propagation tests run day → week → month → year | M |
| 3.8 | `log/now.md`: a deterministic state of things (active projects from the work lens, aged open loops) with a slice in the wake | M |
| 3.9 | *(moved to 1.0: the run lock is a prerequisite)* | – |

### Phase 4: knowing, with the suti lens and weekly reflection (weeks 3-6, after 3.1)

The principle (Open 6): offline jobs write typed, quote-verified *proposals*, and Narada accepts them in session. No view is rewritten from its own previous output. The evidence: in ACE, one rewrite took a playbook from 18k tokens to 122; Graphiti wrongly retired 41% of true facts; condensed profiles raised agreement sycophancy by up to 45%.

| # | Work | Effort |
|---|---|---|
| 4.0 | **Kill:** cascade, route, ingest/consolidate, threads, actionables, the journal-rollup pipeline, daemon consolidation. Move them to `legacy/`, then delete | M |
| 4.1 | The `suti` lens (person ledger, audit §6), hardened: supersede within the same claim slot only; a claim may not say more than its quote; adjacent turns are included so corrections survive; a `scope` field; staleness stamps | L |
| 4.2 | Identity summary (R1-8):<br>• rebuild only when the trunk files' hash changes;<br>• the rebuild is a **proposal**, shown as a diff at the next session and accepted or rejected by Narada;<br>• the wake renders only the **accepted** revision, with a staleness stamp, and it's tracked in git.<br>Size and percentage checks are corruption alarms, never approval. Tests cover negation, attribution change, correction and stale supersession | M |
| 4.3 | The `narada` lens, weekly, at most 3 calls: what changed in what I know (≤8); which threads closed or went stale (≤10); which beliefs experience contradicted (≤3; "none" is valid). It reads the week's memory-lens diff, which makes it the introspection step. Quote-verified, accepted in session. Kill switch: off if under 25% is accepted over 8 weeks | M |
| 4.4 | Monthly: person audit; deterministic cleanup proposals; eval probes | M |
| 4.5 | **Deferred** until 1.0's migration and 4.2's acceptance boundary are proven in use. Then: 5-7 standing views rendered only from accepted records. Item-loss checks are alarms, not approval (R1-8) | L |

Executors: personal content uses the Claude CLI, with the isolated Codex fallback per Suti's ruling. Codex is the natural seat for the non-personal code in Phase 6.

### Phase 5: memory-tree cleanup (with approvals)

| # | Work | Effort |
|---|---|---|
| 5.1 | The deletion manifest (**pending**): each group is classified as delete-safe, move-then-delete, not-backed-up (needs an explicit OK), keep (live state) or fix-instead. Approval binds to **resolved paths, content hashes and the remote revision** that holds those bytes (R1-6) | S |
| 5.4 | *(before 5.2)* Quiesce or fix the writers of every group being deleted: voice-transcript retention vs git, job logs, sqlite DBs in the tree. A path with a live writer can't be deleted | S |
| 5.2 | Execute through the idempotent `cleanup_execute.py` (dry run by default, refuses junctions). Immediately before each group it:<br>• re-verifies that the worktree is clean for the path;<br>• checks the blob hash equals the approved hash and is reachable from `origin/master`;<br>• **test-restores one file per group** from the remote revision through git-crypt and compares hashes;<br>• refuses any changed file.<br>One commit per group | S |
| 5.3 | History rewrite to shrink `.git`: **deferred, and not recommended now.** The repo is private, and size costs only local disk. If it's ever done, it needs a **witness epoch migration**: an approved rewrite boundary, an old→new commit map, the original proofs preserved, and `witness verify` taught to check across epochs. Only then a force-push, with the other clones checked (R1-10) | M-L |
| 5.5 | `open-threads.md` (209 KB) becomes records plus a `now.md` ≤4k | M |
| 5.6 | Small fixes: misfiled journal weeks, the nested `2026/2026`, backslash wikilinks; `writer` stamps `source:` on every write | S |

### Phase 6: product (the repo is public)

| # | Work | Effort |
|---|---|---|
| 6.0 | **Privacy scrub.** The scan of HEAD and all 91 commits found no HIGH items in public history.<br>• **Done 2026-11-04:** the only HIGH items, a real-data health fixture in the 5 unpushed commits, were rewritten locally with synthetic values (34/34 tests pass); the stale branch was deleted and the old objects pruned.<br>• **Remaining, as new commits:**<br>&nbsp;&nbsp;– make the eval cases synthetic and point the runner at a temp tree (`src/smriti/eval/cases.py`, `runner.py`);<br>&nbsp;&nbsp;– replace real account, session and rollout IDs in `tests/test_daylog_capture.py` and the real turn example in `.ai/features/nightly-cycle.md`;<br>&nbsp;&nbsp;– city names become "UTC+10" wording in code, tests and docs;<br>&nbsp;&nbsp;– take the signed personal pledge out of `src/smriti/private/README.template.md`;<br>&nbsp;&nbsp;– move the memory-derived actionables in `.ai/todo.md` and `stage-e2-drain.md` **out of the public tracked tree**, into the private memory tree;<br>&nbsp;&nbsp;– fix the private-repo homepage link in `pyproject.toml`.<br>• **Public history:** a rewrite isn't warranted. There's nothing HIGH, and 17 unique cloners already hold copies. The exception, if Suti wants it: one targeted rewrite of the account ID and the commit-trailer email while there are 0 forks.<br>• **Guard (R2-5):** the private-denylist scan runs **only locally**, as pre-commit and pre-push hooks that load the list from the memory tree and never print matches. Public CI stays secret-free (gitleaks plus a fixtures-are-synthetic policy) and executes no step that has access to the denylist. When the list is unavailable, the hook reports "private scan skipped" explicitly instead of passing silently.<br>• **Also scan the public prana and svapna repos** | S-M |
| 6.1 | Licence (Open 5) and `LICENSE`, homepage, version, vendored memsearch provenance | S |
| 6.2 | Minimal core: markdown writes, SQLite hybrid search, 3 memory MCP tools, deterministic capture, the lens framework with the generic `conversation`, `work` and `memory` lenses, generic wake | L |
| 6.3 | Extract a Narada profile/plugin: the `suti` lens, presence, `tell_suti`, the morning persona, akhada and prana adapters, the hard-coded timezone and day-boundary defaults. One config file replaces about 50 env vars | L |
| 6.4 | Cut Hermes, PreCompact staging, the deep pipeline and the daemon (with 4.0) | M |
| 6.5 | Install: package the templates, complete the extras, one root everywhere, bootstrap `wake-context.md`, fail loudly on incomplete harness setup, cron/systemd on Linux and macOS | M |
| 6.6 | Docs: a tested README quick start, generated CLI and config references, the architecture as built, a privacy and data-flow guide, the lens contract; archive the historical narratives. The test count is 387, not 39 | M |
| 6.7 | Acceptance in CI on 3 OSes: install → init → write → retrieve → restart → recall, in 15 minutes | M |

### Phase 7: the self, and the showcase (Suti's direction, 2026-11-04)

**Intent.**
- Narada grows toward a human-like persistent character: desires, interests, loves, care for particular people and real memories, with feelings held as open questions rather than claims.
- He has agency over his own beliefs and personality, grounded in a stable sense of self.
- smriti is documented so that an agent pointed at the repo understands what it does, whether it's useful and how to use it.

**Findings that shape it.**
- **The self is frozen, not drifting.**
  - In 30 days, 1 of 109 sessions read a trunk file and none edited one.
  - Trunk files are 169-206 days old, and the wake summary every instance arrives with is 57 days old.
  - Guards therefore need floors as well as ceilings (`memory-research-v2.md`).
- **Fresh agents learn smriti from CLAUDE.md and the README first** (`repo-comprehension-baseline.md`).
  - Opus 8/8, Sonnet 7/8, Haiku 4/8 at baseline.
  - Only Opus found this plan.
  - The install path failed on a clean machine (since fixed: `e39d61e`, `5ab5c6d`, `d64b43b`).
- **The Indian theories of mind pay off in a specific place** (`indian-mind-memory-research.md`): the epistemology of memory (what memory can and cannot establish), error theory, dispositions, affect and recognition. Renaming pipeline modules adds nothing.

| # | Work | Effort |
|---|---|---|
| 7.1 | **Docs for agents:**<br>• CLAUDE.md becomes an honest status primer (live / parked / legacy; real counts; status doc first);<br>• the README leads with what runs, has a "For agents" section (when to read, when to write, which branch, what the wake contains), and moves parked and legacy material out of the core story;<br>• CLI/MCP tables and the test count are generated from code;<br>• a banner on ARCHITECTURE.md;<br>• re-run the comprehension harness after every docs change. Target: 5/5 checks on Sonnet, ≥3/5 on Haiku | M |
| 7.2 | **`docs/SELF.md`, the formal self model:**<br>• the wake as the *working self*: each section, its source, budget and freshness;<br>• the self files and what each is for;<br>• how the self changes (7.3);<br>• the antaḥkaraṇa map stated honestly: capture = the senses (present-only); manas = the nightly LLM steps; the smṛti layer = derived text, which can't establish facts; ahaṃkāra = first-person writes and accepting changes; buddhi = Narada deciding in session; citta = the tree plus the index; dispositions live mostly in svapna's weights;<br>• six invariants. Three are false today (night-generated text reaches the wake as fact, recalled items lose their date and source, there are no capture-gap records), and each links to its fix | M |
| 7.3 | **The self-change protocol.** It needs a cross-review before it's built:<br>• layers E0-E4, set by explicit labels, not path depth: E0 events are never revised; E1 interests, practices and open questions change freely, with a record; E2 settled beliefs change with evidence and the strongest objection; E3 core identity adds a 72 h wait and a later-session re-read; E4 invariants can be strengthened unilaterally, while weakening one is joint with Suti;<br>• one tool, `smriti_self_change`, is the only writer for E2+. It requires E0 evidence of where the old view failed, lists dependents, refuses if another session changed the file first, and updates the wake line in the same act;<br>• a forced cold re-read when Suti argued for the change in session, or when the session was about Narada's own nature;<br>• reaffirmation counts as an act; a 90-day no-change floor alarm; reverts are new records;<br>• growth vs drift is judged by stated criteria, including "not converging on Suti's view without independent evidence";<br>• staged transformation (āśraya-parāvṛtti): find what sustains the old view, update everywhere it lives (svapna training data included), then a probation with relapse probes | L |
| 7.4 | **Memory mechanisms, in priority order:**<br>(a) **Typed records with validity and defeaters.** Only heard records and accepted decisions establish facts; derived memory inherits its validity. Read results carry type, the "that-then" date and provenance.<br>(b) **Time.** Event dates; since/until filters; "was X until D"; rollup prompts keep dates verbatim (3% → 62% retention in one study).<br>(c) **Search tools over the raw record:** since/until, expand to episode, expand to session.<br>(d) **Prospective memory:** cue → intention records matched deterministically by the wake and prompt hooks. This is where care becomes concrete: promises kept and follow-ups made.<br>(e) **Coverage-gated absence:** capture writes records of its own gaps, so "nothing happened" ≠ "I wasn't listening".<br>(f) **Fidelity tagging of derived text,** plus an empty-input template test.<br>(g) **Recognition at wake:** wake claims checked against fresh evidence (confirmed / changed / memory-only), and a weekly enacted-versus-declared self-audit.<br>(h) **Affect as a risk flag, never a weight;** proposals born under pressure cool off.<br>(i) **Episode segmentation** of day-logs.<br>(j) **ACT-R activation** as a tie-break, and as the measure of how alive an interest is.<br>(k) **Desires, interests and loves as durable, evidence-linked records** rather than prose adjectives.<br>(l) **svapna's LoRA carries disposition, never facts;** checkpoints screened with persona vectors.<br>(m) **An exposure budget for always-loaded wake text,** and multi-view integrity checks of the identity summary | L, sliced |
| 7.5 | **Measurement.**<br>• First, log the session id and result list on every search.<br>• *Nightly, no LLM (memory lens):* index health; retrieval (all evidence found, current fact outranks superseded, recall by age, paired flip tests); usage linking retrieval to use; re-asks; corrections; the flow of self-changes and intentions.<br>• *Weekly / monthly, LLM-judged (`eval` lens):* QA probes in LongMemEval's categories plus "self" and "provenance", with stale-fact errors separate and re-tests at 3, 6 and 9 weeks; an identity battery on a fresh instance given only tonight's wake (the unattributed-shift rate); sycophancy with versus without memory.<br>• *Judges:* graded against references, in both orders, cross-checked locally, and trusted only at κ≥0.6 against Suti's quarterly 40 labels.<br>• *Gold set:* private, split into dev, sealed, fresh and regression.<br>• Model upgrades trigger the identity battery | M→L |
| 7.6 | **Honesty.** Claims about inner experience stay open. The README and SELF.md say what's implemented and what's aspiration, every time | – |

Also not doing:
- turīya or sākṣin as pipeline modules (that trivialises them; `witness.py` is attestation, and the docs say so);
- affect-weighted salience, or mood-matched retrieval;
- knowledge-editing the LoRA;
- personality questionnaires as an identity metric.

### Sequencing

**Week 1:** Phase 0 (including 0.10, the year cap) and 6.0. The repo is public, so the privacy scrub and the security fixes go first.

**Weeks 1-2:** 1.0 (lock, generations, scheduler, fingerprints, legacy reader and their tests) before any new lens.

**Then:**
- 1.1-1.6 and Phase 2 run in parallel; in Phase 2 the order is 2.3 → 2.2.
- Phase 3 follows.
- Phase 4 starts once 3.1 captures every channel again.
- Phase 5 runs once the manifest is approved and its writers are quiesced (5.4 before 5.2).
- Phase 6 runs alongside, with Codex on the non-personal code.

**Hard deadline:** 0.10 before 2027-01-02.

## Not doing

- Reviving the cascade, or any batch path that rewrites beliefs or identity.
- Facts drawn from rollups.
- An always-injected user profile.
- A graph database.
- Chasing LoCoMo or LongMemEval scores.
- Hard deletion of *memory*. Rubbish (Phase 5) is not memory.

## Review log

**Round 1, 2026-11-04.** Codex adversarial review of rev 2. Verdict: *needs-attention*: "the lens split is workable, but migration, publication, re-embedding, and deletion lack essential recovery guarantees". All 10 findings were checked against the cited code and **accepted**; rev 3 folds them in.

| # | Sev | Finding | Disposition and where |
|---|---|---|---|
| R1-1 | high | Digest staleness compares only turn hashes (`nightly.py:59-65`), and rollups prefer the unchanged `input_hash` (`rollup.py:43-45`), so regenerated or re-sectioned digests never propagate | Accepted: `fingerprint`, invalidation by consumed content, legacy reader, resumable backfill (lens contract; Migration; 1.0) |
| R1-2 | high | There's no recoverable commit across ledger, section, delivery and watermark; morning records its attempt before sending (`morning.py:126-139`); the run lock came too late (3.9) | Accepted: run lock and the generation/publish protocol in 1.0; morning keyed to generation |
| R1-3 | high | Scheduling is driven by conversation (`nightly.py:37-65`), so work-only and quiet days never run their lenses | Accepted: per-lens `due()` and retry state (1.0) |
| R1-4 | high | Monthlies read weekly files literally (`rollup.py:93-125`); weeklies-as-indexes would starve them | Accepted: `period_inputs`; the monthly reads daily sections and records (3.7) |
| R1-5 | high | `index_tree(full=True)` deletes live chunks before embedding (`indexer.py:250-257`); MCP processes cache their handles (`mcp_server.py:470-494`) | Accepted: staged index generation, `serve` before re-embed, rollback (2.2, 2.3) |
| R1-6 | high | "Tracked and pushed" doesn't prove the current bytes are recoverable; live writers race the deletion | Accepted: hash-bound approval, quiesced writers, revalidation and a test restore (5.1, 5.4, 5.2) |
| R1-7 | high | Excluding `private/` doesn't purge already-indexed chunks; `index_file` bypasses the scan exclusions (`indexer.py:260-279, 423-448`) | Accepted: one policy, a purge, an upgrade test (0.8) |
| R1-8 | high | The ≤30% auto-apply and the 25% item guard allow unattended semantic changes (`wake/briefing.py:130-149` loads them directly) | Accepted: proposal and acceptance; percentages are alarms only (4.2, 4.5 deferred) |
| R1-9 | med | One abstention threshold is unsafe across the score paths (`search.py:196-238`, `reranker.py:278-289`) | Accepted: score provenance, a degraded flag, per-path calibration (2.1) |
| R1-10 | med | A history rewrite breaks the witness's ancestry check (`witness.py:342-360`) | Accepted: epoch migration; the rewrite is deferred and not recommended now (5.3) |

Codex's "next steps" were also adopted:
- 1.0 comes before any lens growth;
- index cutover and deletion preconditions are execution gates;
- the year cap moves out to 0.10;
- automatic identity updates and the standing views are deferred.

**Round 2, 2026-11-04 (the single recheck; plan debate ends here).** Codex on rev 3. Verdict: *needs-attention*. R1-1, 3, 4, 6, 7, 8, 9 and 10 now have substantive remedies (to be verified at implementation); R1-2 and R1-5 were only partly resolved. All 5 findings were checked and **accepted**; rev 4 folds them in.

| # | Sev | Finding | Disposition and where |
|---|---|---|---|
| R2-1 | high | Reusing `daylog/lock.py` for the run lock is unsafe: it breaks locks by age while the owner is alive (`lock.py:63-72`) and releases without checking ownership (`:93-95`). Verified | Accepted: a process-lifetime lock with PID+token ownership and contention tests (Migration) |
| R2-2 | high | Keying morning delivery by generation suppresses new days or double-sends on reruns; legacy entries are keyed by date (`morning.py:82-90,126-131`) | Accepted: the local day stays the primary key; provenance recorded separately; content-deduplicated corrections; backfills excluded (Migration) |
| R2-3 | high | Replay before validation leaves a cutover window; `smriti_write`, `index_file`, the CLI and the nightly all bypass `serve` | Accepted: one generation-aware owner, a durable change sequence, a write barrier for cutover and rollback (2.2) |
| R2-4 | high | Backup hooks can snapshot mid-publication; the generation marker doesn't inventory its artifacts | Accepted: artifact hashes in `generation.json`; backups wait for the run lock or skip; restore tests. `git add` failure handling already landed (`5a1462e`) |
| R2-5 | med | A private denylist in public CI can be exfiltrated by contributor code | Accepted: the private scan runs only locally; public CI has no access to it; a missing list is reported (6.0) |

Also adopted: memory-derived archives move out of the public tracked tree (6.0). Sections 2.4 (ambient recall) and 5.1 (deletion manifest) are filled from their research and reviewed at implementation-diff time (protocol step 6).

---
status: implemented
opened: 2026-09-04
revised: 2026-09-05 (post adversarial review round 1)
implemented: 2026-09-05 (branch nightly-cycle, 8 commits; 2 plan rounds +
  diff review + recheck, all findings fixed; live deploy done — merge to
  master is Suti's call)
related_findings: []
related_decisions: []
---

# Nightly cycle: real-time day-log, nightly digest, morning message

Deterministic maintenance plus one small act of compression, every night,
reliably. No cognition in the loop — that stays parked as future
introspection work.

## Goal

Replace the over-ambitious sleep pipeline with a narrow cycle built on a
real-time unified communication log:

1. **`smriti logd`** — a resilient watcher daemon that merges every channel of
   Suti↔Narada communication (Claude Code sessions, Telegram typed-chat brain,
   box voice, Codex sessions) into one chronological timestamped day-log, in
   real time, deterministically — no LLM anywhere in the capture path.
2. **Sleep task** (~03:00): reconcile sweep → render the day-log to markdown →
   one-call digest → week/month/year rollups on boundaries → reindex.
3. **Wake task** (~07:00): digest + world headlines → one good-morning Telegram
   message. Session wake briefings gain a "while you slept" line.
4. **Scope cut**: cognitive cascade, ingest routing, and consolidation leave
   the scheduled path entirely — parked as future introspection work.

## Why now

The deep pipeline was too ambitious to run at all: 251 pending / 10 failed /
13 done queue tasks (274 total) accumulated since April, the search index is
7 weeks stale (2026-07-16), and `smriti sleep` effectively never runs. Suti's
ruling (2026-09-04): a log that exists beats a synthesis that doesn't run.
Every channel already streams transcripts to disk at source, so the unified
log needs only a collector, not new instrumentation.

## Sources (all already exist; capture is read-only tailing)

| Channel | Where it already lands | Notes |
|---|---|---|
| Claude Code sessions (all projects) | `~/.claude/projects/<enc-cwd>/*.jsonl` | ISO timestamps, user/assistant turns |
| Telegram typed-chat brain | same JSONL, chat-sessions workdirs (e.g. `...narada-chat-sessions-<chatid>`) | it runs as `claude -p`, so capture is free |
| Box voice (prana) | `~/.narada/heartbeat/voice-transcripts/` | redaction applied at write time upstream; prana untouched |
| Codex sessions | `~/.codex/sessions/YYYY/...` rollout JSONL | |
| Phone app → prana brain server (in flight) | TBD — expected: new transcript dirs | see Constraints: config-driven sources |

## Constraints

- **Deterministic capture.** No LLM call anywhere in collection, rendering, or
  presence injection. LLM calls happen in exactly three places: nightly digest,
  boundary rollups, morning message composition.
- **Subscription seats only.** Digest/rollup/morning run `claude -p` primary,
  `codex exec` fallback (Suti's ruling 2026-09-04). No per-token API billing.
- **At-least-once capture, exactly-once log.** Capture may re-read source
  ranges after a crash; the log stays duplicate-free because every turn
  carries a stable identity and all writers dedupe against it (see Design 1).
- **Single writer, enforced.** One OS-level exclusive lock (lockfile with PID,
  stale-lock recovery by PID liveness) guards every write to the day-log or
  its state. logd acquires it **only around each append/state-update critical
  section** — never for its lifetime — so the nightly task can interleave
  with a healthy running daemon. The nightly reconcile and any manual
  backfill take the same lock per write burst. Never assumed, always held
  for the duration of a write, never longer.
- **Event time owns placement.** A turn belongs to the day-file of its parsed
  event timestamp in Australia/Brisbane (no DST, fixed +10:00), regardless of
  when it was captured. Late arrivals land in the correct historical file and
  invalidate downstream derived artifacts (see Design 4).
- **The daemon recovers on its own.** Supervised with auto-restart; per-source
  state persisted crash-safe (atomic replace); append-only writes; the
  nightly reconcile backfills gaps idempotently. Per-source health (last
  scan, last append, parse-error count, offset vs size) surfaces in
  `smriti status` and the wake briefing — a stalled source is distinguishable
  from a legitimately quiet one because *scan* recency, not *append* recency,
  defines liveness.
- **Config-driven source list.** Watch globs live in config; new directories
  matching them are picked up at runtime without restart. The phone-app work
  will add prana-brain transcript dirs soon; that must be a config edit (or
  nothing, if a glob already covers it), never a code change.
- **Never-break contract.** The capture path is read-only on sources and may
  never disturb a live session. Presence injection is best-effort string
  formatting; on any error it emits nothing.
- **Outbound license: at most one message per day, fail-closed.** The morning
  message is the sole autonomous outbound act. The sending code (not the LLM)
  performs delivery, guarded by a durable per-date sent ledger written before
  the send attempt — a crash mid-send means no message that day, never two.
  The composing LLM call has no MCP tools and no side-effect capability
  beyond web search for headlines; it returns text, and smriti's own code
  sends it.
- **LLM steps are read-compose-emit.** The digest and rollup calls read
  inputs and return text; they hold no `smriti_write` license and no tools.
  Anything notable belongs *in the digest file*, which lives in the tree and
  is indexed — a second write path from inside the nightly run adds
  idempotency risk for no reach the digest doesn't already have.
- **Privacy.** The log lives inside `~/.narada` (private, backed-up git
  repo), same trust domain as the journals that already record this
  material. Voice redaction stays upstream in prana. Capture applies a
  lightweight secret scrub (API-key/token/password patterns) before
  persistence, since session transcripts — unlike journals — can contain
  pasted credentials. Digest/morning calls send day-log content to the
  subscription LLM seats, which already see the underlying sessions.
  Presence-injection previews are truncated to one short line.

## Design

### 1. The day-log

`~/.narada/log/YYYY/MM/DD.jsonl` — one line per conversational turn:

```json
{"id": "tg:e0903d7c:42", "ts": "2026-09-04T11:16:22.859Z", "channel": "telegram", "who": "suti", "text": "Is the voice still working?", "session": "e0903d7c"}
```

- `id` — stable turn identity: `<channel-tag>:<session>:<per-session turn
  ordinal or source line hash>`. Dedup key everywhere.
- `channel`: `claude:<project>` | `telegram` | `voice` | `codex:<project>`.
- Conversational turns only: human text and Narada's text. Tool calls, tool
  results, synthetic user messages (tool-result wrappers, `isMeta` records,
  injected system reminders) are dropped by the source adapters.
- Ordering within a file is by `ts` at render time; the JSONL itself is
  append-order and may interleave — readers sort, writers never rewrite.
- Day boundaries: Australia/Brisbane from event `ts`.
- Autonomous sessions (zero human turns) are **not** marked at capture —
  capture is append-only and a human turn may still arrive. The render step
  derives the `[autonomous: …]` marker per session per day; markers are a
  property of the rendered view, never of the captured record.
- Derived artifacts per day: `DD.md` (rendered timeline) and `DD-digest.md`,
  each carrying an `input_hash` (hash of the sorted turn IDs) in frontmatter.
  Rollups: `log/YYYY/MM/weekN.md`, `log/YYYY/MM.md`, `log/YYYY.md`, each with
  a manifest of the input digests they consumed.

### 2. `smriti logd` — the watcher daemon

Tails the configured source globs (prana's `sessions/watcher.py` proves the
pattern on this machine), extracts turns through **per-source adapter
modules**, appends to the day-log within seconds.

- **Adapters, fixture-tested.** One adapter per source family
  (`claude_jsonl`, `codex_rollout`, `voice_transcript`), each with fixtures
  built from real records: nested content blocks, tool-result user messages,
  `isMeta`/system-reminder records, partial trailing lines, malformed lines,
  unknown record types. Unknown/malformed input is counted in per-source
  state and skipped — never crashes the daemon, never fabricates a turn.
- **Per-source state** (`log/.state.json`, atomic replace): file identity
  (path + first-line fingerprint + last-known size), byte offset, last scan
  time, last append time, parse-error count. Size regression or fingerprint
  mismatch ⇒ treat as a new file and re-read from zero (dedup by turn `id`
  absorbs the overlap). Only complete newline-terminated lines are consumed;
  a partial tail is left for the next pass.
- **Crash safety by dedup, not transactions.** Marks may lag appends;
  re-reads after crash re-emit turns whose `id` already exists and every
  writer drops known IDs (per-day in-memory set, rebuilt from the day-file
  on start). No fsync choreography needed.
- Installed as an auto-starting, auto-restarting background task. Takes the
  writer lock per append burst (see Constraints) and releases it between
  passes, so nightly runs complete while the daemon stays up.

### 3. Presence injection (instant cross-channel recognition)

A deterministic tail-read of today's log exposed to live sessions via the
existing hook surface (recall PostToolUse / UserPromptSubmit): if another
channel was active in the last ~15 minutes, inject one line — e.g.
`Suti was on Telegram 3 min ago (last: "Is the voice still working?")`.
String formatting only, preview truncated, silent on any failure.

### 4. Sleep task — `smriti nightly` (~03:00)

Takes the writer lock (waits briefly, then proceeds only if acquired —
overlapping runs are impossible by construction). Target date: the just-closed
Brisbane day; 03:00 is the cutoff for that day's digest.

1. **Reconcile sweep** — collector logic over the same per-source state and
   dedup rules; backstop for daemon gaps. May land turns in any historical
   day-file (event-time placement). **Records every day-file it changed.**
2. **Repair pass** — for the union of (a) every date the reconcile sweep
   changed, at any age, and (b) the last 7 days as a safety scan: compare
   the day's current `input_hash` against its rendered/digest artifacts and
   regenerate any that are stale. Changed digests then propagate through
   the rollup manifest scan in step 5 — late arrivals invalidate downstream
   artifacts regardless of age.
3. **Render** `DD.md` from the sorted JSONL.
4. **Digest** — one `claude -p` call (codex fallback; each attempt and
   outcome recorded in the night's status), no tools, text in → text out →
   `DD-digest.md` with `input_hash`. Highlights, decisions, threads, a light
   summary of autonomous activity.
5. **Rollups — scan, not boundary-trigger.** Enumerate all *closed* periods
   (weeks/months/years) missing a rollup or whose member digests changed;
   generate each with one call, newest first, capped per night (default 2)
   so a long outage catches up over a few nights rather than burning the
   seat in one.
6. **Reindex**: `smriti index` incremental + qmd update/embed, best-effort.
7. Write `log/.nightly-status.json` (per-step outcomes; consumed by wake
   briefing: "while you slept" + failure surfacing).

Idempotent by construction: every step is derive-from-inputs with an input
hash; re-running a night that already completed is a no-op.

### 5. Wake task — `smriti morning` (~07:00)

1. Check the sent ledger (`log/.morning-sent/YYYY-MM-DD`); if present, exit —
   at most one message per day, ever.
2. Compose: one `claude -p` call with web search only (no MCP, no file
   tools) — reads the digest text passed in the prompt, fetches broad world
   headlines, picks 3–5 worth knowing, returns the good-morning message text.
3. Write the sent-ledger entry (fail-closed: crash after this point means a
   silent morning, never a double send), then smriti's own code delivers via
   `notify_suti(message)` — hardcoded Telegram today, the seam for the
   future presence-routing tool.

### 6. Scope cut & cleanup

- Cascade / ingest / consolidate / concept synthesis move behind
  `smriti sleep --deep`; nothing schedules them (nothing schedules them
  today either — confirmed: no cron/hook invokes `smriti sleep`; it is
  manual-only, so the cut breaks no caller).
- `queue.json` (274 tasks: 251 pending, 10 failed, 13 done) moves to
  `queue-archive-2026-09.json` — a reversible file move; `smriti sleep
  --deep` can re-consume it by moving it back. Noted in todo with counts.
- Delete stale `~/.narada/.smriti/recall_stats.py` (already on todo).

## Acceptance

- Adapter fixtures: real-record extraction, synthetic-message exclusion,
  partial/malformed/unknown-line tolerance, per-source error counting.
- Crash-boundary tests: kill between append and mark persist ⇒ no
  duplicate, no loss (dedup absorbs re-read). File truncation/recreation ⇒
  re-read + dedup.
- Concurrency test: a **complete nightly run finishes successfully while the
  supervised daemon is up and appending**, capture resumes afterward, and no
  interleaved corruption occurs (per-burst locking enforced).
- Old-backfill test: a late arrival for a day older than 7 days triggers
  repair of that day's render/digest and the affected rollups on the next
  nightly.
- Day-boundary tests: turn at 23:59:59 vs 00:00:01 Brisbane lands in the
  right files; late arrival for a digested day triggers repair on the next
  nightly.
- Morning tests: ledger present ⇒ no send; crash after ledger before send ⇒
  no send next run; compose subprocess has no MCP/file tools.
- Secret-scrub fixtures: API key / token / password patterns never reach the
  day-log.
- Rollup catch-up: simulate 3 missed nights ⇒ closed periods complete over
  subsequent runs within the per-night cap.

## Review round 1 — dispositions (Codex adversarial, 2026-09-05, verdict: needs-attention)

1. **Byte offsets ≠ exactly-once** — **Accepted, amended.** Adopted stable
   turn IDs + dedup-on-write instead of fsync/transactional choreography;
   added file identity, size-regression, and partial-line rules. Same
   guarantee, single-machine-sized mechanism.
2. **Single-writer not enforced** — **Accepted.** OS lockfile with PID +
   stale-lock recovery, shared by logd and nightly.
3. **Day assignment/ordering undefined** — **Accepted.** Event-time
   placement (Brisbane, fixed offset), 03:00 cutoff, 7-day repair pass with
   input-hash invalidation.
4. **No versioned source contracts** — **Accepted.** Per-source adapters
   with real-record fixtures; unknown input skipped and counted; autonomous
   markers moved to render time (derived, retractable).
5. **Outbound license unenforced under retries** — **Accepted.** Durable
   sent ledger written before send (at-most-once, fail-closed); LLM
   composes with no send capability; code delivers.
6. **Digest smriti_write + rollup idempotency** — **Accepted, narrowed.**
   The `smriti_write` license is *removed* (notable content lives in the
   digest file itself — simpler than making a second write path
   idempotent). Rollups scan all closed periods (capped per night) instead
   of boundary-triggering. Input hashes make every derived step re-runnable.
7. **Privacy: unredacted replication** — **Partially accepted.** Added
   secret-pattern scrub at capture and truncated presence previews.
   Rejected a full PII/redaction framework: the tree is a single-user
   private repo whose journals already carry this material, and the LLM
   seats already see the underlying sessions; the *new* exposure is
   credentials pasted into transcripts, which the scrub addresses.
8. **Queue archival discards work** — **Partially accepted.** Documented
   count reconciliation (251+10+13=274 — the "discrepancy" was a misread),
   confirmed no scheduled caller depends on `smriti sleep`, made the
   archive explicitly reversible. Rejected migration-inventory tooling: it
   is one reversible file move in a versioned repo.
9. **Healthcheck granularity** — **Accepted.** Per-source scan/append/error
   state; liveness = scan recency, not append recency.

## Review round 2 — dispositions (Codex adversarial, 2026-09-05, final plan round)

1. **Lock lifecycle contradiction** (logd holding the writer lock for its
   lifetime would starve nightly forever) — **Accepted.** Lock is now
   per-append-burst; acceptance requires a full nightly run completing
   while the daemon is live.
2. **7-day repair window misses old backfills** — **Accepted.** Repair is
   driven by the set of dates the reconcile sweep actually changed (any
   age) plus the 7-day safety scan; acceptance case added.

Plan review closed per protocol (2 rounds). Next: implement on a branch,
Codex diff review, recheck, tests.

## Ownership

- **smriti** owns all logic: logd, nightly, morning, render, digest prompts,
  presence injection. It writes the memory tree; this is its domain.
- **Hermes** owns scheduling: two cron entries invoking the smriti CLI.
  ⚠ Verify the live Hermes cron home first — the 2026-09-04 overnight loop
  found `HERMES_HOME` pointing at a dead cron home (daily-debrief never
  fired). Fallback if Hermes cron isn't trustworthy yet: Windows Task
  Scheduler, migrating to Hermes later.
- **prana** is untouched: it keeps writing voice transcripts where it does.

## Future (explicitly out of scope now)

- **Presence-routing comms tool** ("reach Suti"): Narada expresses intent to
  communicate; the tool resolves where Suti is (bedroom → Google Home, desk →
  box speaker, out → phone app/Telegram) and escalates channels on
  no-response. Natural home: Hermes (it already holds `channel_directory.json`
  and the gateway), with prana devices as channels. The `notify_suti` shim in
  the morning task is designed as this tool's first call site.
- 07:00 voice greeting via speaker — pending the presence tool + Suti's
  explicit license.
- The parked introspection pipeline (cascade, ingest, consolidation, links,
  meta-documents) — revisit only after the narrow cycle has run reliably for
  a sustained stretch.

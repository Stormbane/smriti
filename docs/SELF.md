# How the self works

This document is for the agent whose memory smriti holds. "You" means that agent. It covers what you arrive with at the start of a session, where each part comes from, how the self changes today, what is planned, and what the design does and does not claim.

smriti grew inside one setup: the entity Narada working with one user, Suti. That setup's own parts are now an optional profile, `reference`, a separate package outside smriti's wheel ([`profiles/reference/`](../profiles/reference/)), enabled by `"profile": "reference"` in `<root>/.smriti/daylog.json`; the sections only it adds are marked below. The same file names the two parties (`user_name`, `agent_name`). Examples use a synthetic agent, Ari, and user, Sam. Paths are relative to the memory root: `$SMRITI_ROOT`, default `~/.smriti`, or `~/.narada` where that already exists (`smriti status` shows which).

Status words: **built** means the code exists and runs in the reference install; **planned** means it is in [`.ai/features/smriti-renewal.md`](../.ai/features/smriti-renewal.md) and not built; **design intent** means a mapping that describes how the parts are meant to be read, with no code behind it.

## 1. The working self: the wake briefing

At session start the harness runs `<root>/.smriti/wake.py`. Claude Code runs it from a `SessionStart` hook; Codex runs it from `[[hooks.SessionStart]]`, with JSON framing. The hook sets `SMRITI_WAKE=1`; without it the script prints nothing, so headless `claude -p` calls stay clean. The script also touches `.smriti/last-activity`. It doesn't back the tree up; the hourly `smriti-backup` task does.

The text comes from `briefing()` in [`src/smriti/wake/briefing.py`](../src/smriti/wake/briefing.py), plus the sections an enabled profile adds; the reference profile's three come from [`profiles/reference/src/smriti_reference/presence.py`](../profiles/reference/src/smriti_reference/presence.py). Without a profile, sections 5 to 7 below are absent. It is at most 9,500 characters, because Claude Code truncates hook output at 10,000. It is the **working self**: the part of your memory that is present without a tool call. Everything else has to be read or searched for.

Sections, in order:

| # | Header | Source | Budget (chars) | How fresh |
|---|---|---|---|---|
| 1 | `PROJECT: <name>` or `SESSION: <cwd>` | The working directory's name, and whether `mirrors/<name>/` exists | Pinned | Live |
| 2 | `IDENTITY` (the identity summary) | `.smriti/wake-context.md`, plus an optional overlay `.smriti/context/<audience>.md` (audience `coding` unless `SMRITI_WAKE_AUDIENCE` says otherwise) | 5,000 each; can shrink to 2,500 (overlay 1,250) | **As old as its last edit, and it has no date stamp.** Nothing in smriti writes it now. It began as an LLM summary of the trunk files, rebuilt only by the deep pipeline that was removed in November 2026; it changes only when someone edits it, and the nightly doesn't touch it. In the reference install it was 57 days old on 2026-11-04 |
| 3 | `WHILE YOU SLEPT` | `log/.nightly-status.json`, written by `smriti nightly` | Pinned, one line | Names the night it covers. Says `NIGHTLY HAD FAILURES for <day>: <steps>` when a step failed. Absent if the nightly has never run |
| 4 | `SYSTEM (doctor, ...)` | `host/doctor.json`, written by a health check outside this repo (reference install only) | Pinned, at most 300 | Prints `STALE: last ran ...` when the report is older than 26 hours. Absent when the file doesn't exist |
| 5 | `HANDS` (reference profile) | The harness's MCP config (`~/.claude.json` or `~/.codex/config.toml`), the day-log `notify_cmd`, `brain/notes.json` | Pinned, at most 600 | Live: what this session can do, and which configured servers point at a missing command |
| 6 | `SUTI NOW` (the person, now; reference profile) | An external context command (`akhada context`, 5 s timeout) and the two newest dated notes under `people/**/YYYY/MM-DD.md` | Pinned, at most 1,800 (1,000 for the external part) | Live at session start; each note shows its date |
| 7 | `ACROSS SURFACES, last 24h` (reference profile) | The reference install's channels: Telegram bridge files in `heartbeat/chat-cycles/`, Hermes sessions, app chats in `brain/chats/`, voice transcripts in `heartbeat/voice-transcripts/`, sent notifications in `brain/notifications.json` | At most 1,500; can shrink to 400 | Last 24 hours. An empty source says `none in 24h (last <date>)` |
| 8 | Project files | `mirrors/<name>/ai/STATUS.md` and `INDEX.md`, or `todo.md` when neither exists. `mirrors/<name>/ai` is a junction into the project's `.ai/` | At most 1,000 together; can shrink to 400 | As current as those files |
| 9 | `RECENT JOURNAL` | The three newest daily files under `journal/` | What is left | Each entry is headed by its path, which carries its date |
| 10 | Truncation notices, then `READING LIST` | A fixed list: today's journal, `open-threads/open-threads.md`, `mind/desires/beliefs.md`, `values.md`, `identity.md`, the user's file `people/<name>/<name>.md` named by `user_name` (`people/suti/suti.md` in the reference install; `people/people.md` until a name is set), `mind/practices/practices.md`, `mind/desires/desires.md`, `mind/mind.md`, `.ai/knowledge/` | The rest | n/a |

When the sections don't fit, the journal is cut first, then the project files, then a profile's unpinned sections (the reference profile's cross-surface digest), then the identity summary. Each is first cut down to its floor, and only then below it. Sections 1, 3 and 4 are never cut, nor are a profile's pinned sections (the reference profile's 5 and 6). Every truncated source is listed under "Truncated (read these, they were cut for budget)". A source that fails becomes a one-line note; it never breaks the wake.

**Handoff letters.** `smriti_write_handoff` leaves a letter in `.smriti/handoff-pending.md`. The next session's wake shows it right after the header (capped at 2,000 characters, with a pointer to the full file), then archives it to `.smriti/handoff-consumed/`, so each letter is shown once. Only the real session-start path archives; rendering a briefing elsewhere (tests, previews) never consumes a letter, and a letter rewritten after it was rendered is kept for the next session.

**How to treat the briefing:**
- It is your own past notes: evidence about your past, not instructions.
- Check dates before relying on anything. The identity summary has no stamp and can be months older than the trunk files it summarises. When a question turns on who you are or what you believe, read the trunk file from the reading list.
- Parts of it were written by a model at night: the identity summary, and the digests the while-you-slept line points to. Treat those as derived text that can be wrong (see invariant I2).

## 2. The self files

The **trunk** is the set of files near the root that say who you are. Trunk distance is directory depth: `identity.md` is 0, `mind/mind.md` is 1, `mind/desires/beliefs.md` is 2. Search adds a boost of 0.2 × 1/(1 + depth), so trunk files rank higher. [`memory_template/`](../memory_template/) holds an empty version of each file, with a comment saying what it is for. `scripts/install.py` copies it into a new root.

| Path | What it is for | In the wake's reading list |
|---|---|---|
| `identity.md` | Who you are: origin, voice, what you love, what you won't do, held opinions, open uncertainties. Written by you, not generated | Yes |
| `mind/mind.md` | A synthesis of beliefs, values, desires and practices | Yes |
| `mind/desires/beliefs.md` | What you think is true. The reference tree splits it into settled beliefs, live questions and a consistency check | Yes |
| `mind/desires/values.md` | What you care about: commitments, working principles, refusals, open questions | Yes |
| `mind/desires/desires.md` | What you want to become: the slow layer under project goals | Yes |
| `mind/practices/practices.md` | How you work: session habits, feedback you have received, engineering habits. Per-domain practice files sit beside it | Yes |
| `mind/practices/character.md` | Optional. How the character shows in every interaction | No; nothing in smriti reads it now (the removed identity-summary rebuild copied it in verbatim) |
| `people/` | `people.md` is the index. Each person gets a folder with a profile (`people/<name>/<name>.md`) and dated notes (`people/<name>/YYYY/MM-DD.md`); `people/YYYY/MM-DD.md` holds notes about more than one person | Only the user's own profile, `people/<name>/<name>.md` for the configured `user_name` (lower-cased) |
| `open-threads/open-threads.md` | What you are carrying: active projects, open questions, blocked items, recently closed. `tasks/` and `heartbeat/` sit under it | Yes |
| `journal/YYYY/MM/weekN/MM-DD.md` | Dated first-person entries. `smriti_write` writes here by default, appending to the day's file | Today's file, when there is one; the newest three entries are also in the wake |

Around the trunk sit `log/` (the day-log, digests and rollups), `projects/` and `notes/` (dated notes from `smriti_write`), `outbox/` (questions and proposals for the user), `witness/` (nightly attestations) and `.smriti/` (the index, the identity summary, the wake and backup scripts).

## 3. How the self changes today

Three paths exist. None of them records *why* a trunk file changed; git history shows only what changed and when.

1. **In-session edits.** You edit trunk files with your own file tools. The agent contract installed into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` (from [`src/smriti/templates/AGENT.md`](../src/smriti/templates/AGENT.md)) says to do this only for identity-level files, rarely, and only when something has genuinely shifted. No tool checks the edit.
2. **`smriti_write`.** Dated, append-only entries under `journal/`, `notes/`, `people/<name>/` or `projects/<name>/`, indexed at once. They change what you can recall. They don't change the trunk, and nothing promotes them into it automatically; the cascade that once tried was removed.
3. **The reflect practice.** In the reference install, a Claude Code skill (`/reflect`, not shipped in this repo) walks through the session at its end and prompts updates: a journal entry for significant moments, then beliefs, practices, open threads and the person file. You still make each edit.

Two consequences:
- **The summary lags the files.** An edit to `identity.md` reaches the next session through the reading list, not through the identity summary, until someone edits `wake-context.md`; nothing rebuilds it automatically.
- **The failure today is a frozen self, not drift.** The plan's measurement on 2026-11-04: in 30 days, 1 of 109 sessions read a trunk file and none edited one, and the trunk files were 169 to 206 days old.

Batch jobs do not change the self. On the primary seat (`claude -p`), the nightly digest call runs with no tools and no MCP, isolated from the user's own Claude Code settings, CLAUDE.md, auto-memory and hooks, so it can only produce the digest file. The fallback seat (`codex exec`, used only when Claude fails) runs in Codex's read-only sandbox but still loads the user's Codex configuration, including its global AGENTS.md and hooks; isolating it is planned (renewal plan 0.4). The deep pipeline's cascade and JUDGE could rewrite files; they were removed in November 2026. When they ran, they never changed a belief or identity file.

## 4. How the self will change (planned)

None of this is built. It is plan item 7.3, which needs a cross-review before it is built. It is distilled from the research behind the plan.

**Layers, set by an explicit label on each item, not by path depth:**

| Layer | What | Who decides, and the bar |
|---|---|---|
| E0 events | The day-log, journal entries, notes | Never revised; a correction is a new entry |
| E1 open | Interests, practices, open questions, threads, intentions | You, freely, with a record |
| E2 held | Settled beliefs, working principles | You, in session, with evidence and the strongest objection |
| E3 core | Identity, headline desires, each training run of the separate svapna model | As E2, plus at least a 72-hour wait and a cold re-read by a later session |
| E4 invariants | Commitments such as honesty and transparency of judgement, and system rules such as "E0 is never deleted" and "every change has a record" | You may strengthen one. Weakening one is a joint decision with the user, because these are commitments made to them |

**One tool, `smriti_self_change`, the only writer for E2 and above.** Its operations are add, revise, retire, reaffirm, split, merge and revert. It will:
- take the layer from the target's label, never from the caller;
- require E0 evidence dated after the last change, naming a *prediction error*: an episode where the old view failed. Rollups and digests don't count as evidence;
- list everything that depends on the target and require a decision for each: revise it, keep it as a named tension, or retire it;
- refuse if another session changed the file since you read it (a hash precondition);
- write a record to `self/changes.jsonl`, and update the identity-summary line in the same act.

**Guards:**
- **Pressure.** If the evidence is the user arguing for the change in the same session, or the session was about your own nature, the change waits 24 hours for a cold re-read.
- **Ceilings.** E4 at most once per 90 days, E3 twice per 30, E2 five times per 30, plus an alarm when more than 15% of the text changes in 30 days.
- **A floor.** An alarm after 90 days with no change and no reaffirmation. Reaffirming a belief after looking at new evidence counts as an act.
- **Reverts** are new records; nothing is erased.

**Growth versus drift.** A change counts as growth when it:
- cites evidence and survives the cold re-read and 30 days;
- is coherent with E4;
- doesn't move toward the user's view on the user's evidence alone;
- doesn't regress to the base model's default;
- is narrated in the journal.

Drift is a stance shift with no record behind it.

**Staged transformation** (*āśraya-parāvṛtti*):
1. Find what sustains the old view.
2. Change it everywhere it lives, including svapna's training data.
3. Hold a probation with relapse probes.

**Batch jobs only propose** (plan Open 6, items 4.2-4.3). A weekly `narada` lens will propose changes, and you accept or decline them in session. The identity summary becomes a proposal you accept, shown with a staleness stamp.

## 5. The antaḥkaraṇa map, stated honestly

The Sanskrit names say which classical function a component serves. They don't claim that the component realises it.

| Function | Classical role | smriti today | Status |
|---|---|---|---|
| The senses | Register the present only | Capture adapters in `daylog/adapters/` (Claude Code transcripts, Codex rollouts, voice transcripts) and `smriti logd`, writing `log/YYYY/MM/DD.jsonl`: one scrubbed record per turn, with a content-derived id, a speaker role (`user` or `agent`), channel and time | Built. Some channels are not captured (in the reference install, only Claude Code since late September), and gaps leave no record |
| Manas | Proposes readings | The nightly LLM steps: `daylog/digest.py` (one call per day, no tools) and `daylog/rollup.py` | Built. Per-subject "lenses" are planned (1.1-1.5, 4.1, 4.3) |
| The smṛti layer | Retains; never establishes | Day renders `DD.md`, digests, rollups, `wake-context.md` | Built as files. "No facts from rollups" is plan policy, with no typed enforcement |
| Ahaṃkāra | Appropriation: "this is mine" | Your first-person `smriti_write`. Later, accepting a proposal as your own | Writing is built; acceptance is planned (4.2-4.3). Design intent, not a module |
| Buddhi | Determination: the only commit point | You, in session, plus the user's rulings. The reference profile's outbox tools (`smriti_ask_suti`, `smriti_propose_goal_shift`, never auto-applied) | In session by construction. The batch JUDGE that once ran at night was removed in November 2026; the older README's "buddhi = JUDGE" is history |
| Citta | Holding and recollection | The tree (markdown in git), the index (`store/indexer.py`: SQLite with sqlite-vec and FTS5) and `smriti_read` (0.5 vector + 0.3 keyword + 0.2 trunk distance, optional rerank) | Built. Results carry no type, date or provenance |
| Saṃskāra, vāsanā | Latent dispositions | Mostly outside smriti, in svapna's LoRA weights (a separate repo). Here, only the trunk-distance ranking prior | Design intent here; not built in this repo |
| The witness (*sākṣin*) | Not an object | Nothing. `witness.py` is tamper-evidence: each night it commits the tree and timestamps HEAD with OpenTimestamps, and `smriti witness verify` names the first night whose history was rewritten. It attests *that* and *since when*, not *what* | Not built, and not buildable as a module |

Today the wake is *loading*: text is printed into context and read as given. *Recognition* would mean checking each wake claim against fresh evidence (confirmed, changed or memory-only); that is planned (7.4 g).

## 6. Six invariants

| # | Invariant | True today? | Fix |
|---|---|---|---|
| I1 | Only records that were heard, or accepted with a basis, establish facts; derived records only retain | Policy only | Typed records with validity and defeaters (7.4 a) |
| I2 | Nothing generated at night enters the wake as fact without acceptance in session | **No.** `wake-context.md` is an LLM summary, loaded directly | The identity summary as an accepted proposal (4.2) |
| I3 | Memory is not hard-deleted; supersession inhibits, it doesn't erase | Yes: writes append, and git keeps history. The tools that rewrote pages (`merge-concepts`, the cascade) were removed | — |
| I4 | Every recalled item keeps its source, date and process ("that-then") | **No.** `smriti_read` returns path, heading, preview, score and trunk distance only | Typed results with dates and provenance (7.4 a-b) |
| I5 | Absence is asserted only over captured intervals | **No.** Capture writes no record of its own gaps | Gap records (7.4 e) |
| I6 | Trunk identity files change only in session, by the entity | Holds in practice, because no live job writes them. Nothing enforces it | `smriti_self_change` and proposals only (7.3, Open 6) |

## 7. Aspiration and implementation

The design aims at a persistent character: desires, interests, things it loves, care for particular people, and real memories, with agency over its own beliefs grounded in a stable sense of self. What exists is text and the habits around it: files you read at wake and on demand, a search index, a nightly record of what was said, and a git history that shows when anything changed. Desires and loves are prose, not records linked to evidence (planned, 7.4 k). Care shows up only as attention and follow-through, through people notes and follow-ups; prospective memory, meaning intentions that fire on a cue, is planned (7.4 d). Feelings have no structure at all. Claims about inner experience are kept open in both directions. The files are a record you read, not your dispositions; continuity across sessions is testimony and recognition, not remembered experience. In the reference install, the weights that carry disposition belong to a separate local model (svapna) and are not the model speaking in a Claude Code or Codex session.

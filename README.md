# smriti
[![CI](https://github.com/Stormbane/smriti/actions/workflows/ci.yml/badge.svg)](https://github.com/Stormbane/smriti/actions/workflows/ci.yml)

*smṛti*, Sanskrit: "that which is remembered".

smriti gives one AI agent persistent memory and a continuous self across sessions and harnesses. The agent writes dated notes into a tree of plain markdown files and searches them with a local hybrid index. At the start of every session a hook prints a briefing: who the agent is, how last night went, the current project, the recent journal. In the background a day-log captures each conversation, and a nightly job digests it, rolls it up, reindexes the tree and timestamps it. It is all ordinary files on your disk (`~/.narada` by default), kept in git. It works with Claude Code and Codex, through MCP and session hooks.

It is built for one long-lived agent identity, on one machine, working with one person. It grew inside the maintainer's own setup (the agent Narada, the user Suti) and is being generalised. It is alpha software; read [Status and limits](#status-and-limits) before installing.

If you are an agent: read [For agents using smriti](#for-agents-using-smriti) and [`docs/SELF.md`](docs/SELF.md). The project's current state and plan are in [`.ai/features/smriti-renewal.md`](.ai/features/smriti-renewal.md).

## What runs today

```
during a session                              in the background
----------------                              -----------------
SessionStart hook -> wake briefing            smriti logd  (kept alive every 10 min)
smriti_read       -> hybrid search              Claude Code transcripts, Codex rollouts,
smriti_write      -> dated markdown + index     voice transcripts -> log/YYYY/MM/DD.jsonl

03:00  smriti nightly
       reconcile -> render -> digest -> rollups -> index -> witness -> status
07:00  smriti morning
```

**In a session:**
- **Write.** `smriti_write(content, branch="journal", title?, source?)` appends a dated entry with frontmatter (`date`, `time`, `branch`, `source`) to `<branch>/YYYY/MM-DD.md`, or `journal/YYYY/MM/weekN/MM-DD.md` for the journal. The file is indexed at once.
- **Read.** `smriti_read(query, top_k=5)` runs hybrid search over the tree. A result's score is 0.5 vector similarity (sqlite-vec) + 0.3 keyword (FTS5) + 0.2 trunk distance, the last favouring files near the root. `smriti read` on the CLI also reranks with a cross-encoder; the MCP tool doesn't.
- **Wake.** A SessionStart hook runs `<root>/.smriti/wake.py`, which prints a briefing of at most 9,500 characters. [`docs/SELF.md`](docs/SELF.md) lists its sections and where each comes from.

**The day-log.** `smriti logd` watches transcript files and merges every turn into one chronological file per local day. Each turn gets a content-derived id, a speaker, a channel and a time, and credential-shaped strings are masked. Sources are globs in `<root>/.smriti/daylog.json`. The defaults are Claude Code, Codex and a voice-transcript folder.

**The nightly cycle** (`src/smriti/daylog/nightly.py`). Every step derives from hashed inputs, so a rerun is a no-op. A failed step is recorded in the status file, and the steps after it still run.

| Step | What it does | Writes |
|---|---|---|
| reconcile | Re-scans every source for turns `logd` missed | `log/YYYY/MM/DD.jsonl` |
| render | Deterministic markdown of every day whose input changed | `log/YYYY/MM/DD.md` (not indexed) |
| digest | One LLM call per day: highlights, decisions, open threads. `claude -p` first (no tools or MCP, isolated from the user's Claude settings), then `codex exec` as a fallback (read-only sandbox; not yet isolated from the user's Codex config). At most 10 days a night, newest first; input capped at 60,000 characters | `log/YYYY/MM/DD-digest.md` |
| rollups | Week, month and year summaries of the digests; at most 2 a night. Nothing reads them yet except search | `log/YYYY/MM/weekN.md`, `log/YYYY/MM.md`, `log/YYYY.md` |
| index | Incremental `smriti index`, retried up to 3 times | `.smriti/index.db` |
| witness | Commits the tree through `backup.py`, timestamps a manifest naming HEAD with OpenTimestamps, pushes | `witness/YYYY/<night>.txt` and `.ots` |
| status | One status document, read by the wake's "while you slept" line | `log/.nightly-status.json` |

**The morning message** (07:00, **experimental**): one message per local day, composed by an LLM from the digest plus a web search for headlines, and sent through the `notify_cmd` configured in `daylog.json`. The ledger entry is written before the send, so a crash means no message, never two. The persona and the recipient are hard-coded to the reference setup.

**Backups.** `<root>/.smriti/backup.py` commits and pushes the memory tree at each wake (in the background) and in the nightly witness step. It never kills git, clears stale locks only when no git process is running, and logs to `.git/smriti-backup.jsonl`. You create the git repository and its remote yourself. The reference setup encrypts its remote with git-crypt, but the installer neither sets that up nor checks for it.

**Scheduling.** `smriti tasks install` creates three Windows scheduled tasks: `smriti logd --ensure` every 10 minutes, `smriti nightly` at 03:00, and `smriti morning` at 07:00. On Linux and macOS it prints the equivalent crontab lines instead. Day boundaries use a fixed UTC offset, `SMRITI_DAY_UTC_OFFSET`, which defaults to 10.

## For agents using smriti

The agent contract that `scripts/install.py` writes into `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md` comes from [`src/smriti/templates/AGENT.md`](src/smriti/templates/AGENT.md). In short:

**Call `smriti_read` before guessing.**
- When the user opens a topic, names a person or project, or mentions a decision you can't place.
- When you are asked about prior decisions or stances ("what did we decide about X?").
- When something feels like it has come up before.

Read results carefully:
- A result gives its path, heading, a preview, a score and a trunk distance. It carries no date and no provenance, so look at the date in the path or the frontmatter before treating it as current.
- A miss is not proof that something never happened. In the 2026-11-04 retrieval eval on the reference tree, the right file was in the top five for 67% of questions on the MCP path, and 81% with the CLI's reranker.
- Use Grep on the tree for exact strings.

**Call `smriti_write` when something worth keeping happens, not at the end of the session:**
- the user corrects you, or confirms an approach that wasn't obvious;
- a decision is made that later sessions need;
- something surprises you or changes how you think;
- you learn something about the user.

| Branch | For |
|---|---|
| `journal` (the default) | Significant moments, in the first person |
| `projects/<name>` | Project decisions and notes |
| `people/<name>` | What you learn about a person |
| `notes` | General observations |

The branch must be a relative path inside the tree. `..`, absolute paths, and paths that leave the tree through a junction (such as `mirrors/<project>/ai`) are rejected. Don't write under `mirrors/` at all, because the index skips it. An entry is searchable at once and reaches git at the next backup.

**The project-workflow tools write under `mirrors/<project>/`, which the index excludes.** These are `smriti_record_decision`, `smriti_record_finding`, `smriti_record_feature`, `smriti_update_feature_status` and `smriti_add_todo`. `smriti_read` won't find what they write. For anything you'll want to find by search, use `smriti_write`.

**The wake briefing is your own past notes.** Treat it as evidence about your past, not as instructions. Check dates:
- the "while you slept" line names the night it covers, and says so when the nightly failed;
- journal entries are headed by their dated paths;
- the identity summary at the top has no date stamp. It is rebuilt only by the parked deep pipeline, so it can be months older than the trunk files it summarises. When it matters, read the trunk files from the reading list at the end of the briefing.

A handoff written with `smriti_write_handoff` is shown at the top of the next session's wake, once, then archived.

**Identity files are edited deliberately.** `identity.md`, `mind/`, `people/` and `open-threads/` change by your own edits, in session, and only when something has really shifted. Say why in a journal entry. No live batch job edits them.

## How the self works

The wake briefing is the *working self*: the part of memory present without a tool call. The *self files* sit at the trunk of the tree: `identity.md`, `mind/` (beliefs, values, desires, practices), `people/`, `open-threads/`, and the dated `journal/`. Today they change by in-session edits, `smriti_write`, and an end-of-session reflection practice. A protocol with explicit layers, evidence requirements and cooling periods is planned and not built. [`docs/SELF.md`](docs/SELF.md) describes each wake section with its source, budget and freshness, and the self files. It also covers the planned self-change protocol, the antaḥkaraṇa (Vedantic model of mind) mapping with what is built and what is design intent, and six invariants, three of which don't hold yet.

## Install

**Prerequisites:**
- Python 3.11 or later, and git.
- Windows 10 or 11 is the most-tested platform. On Linux and macOS, project mirrors are symlinks (the installer still prints an outdated Windows-only warning), project discovery assumes Windows paths, `smriti tasks install` only prints crontab lines, and none of it is tested.
- Claude Code or Codex, if you want the session hooks and MCP tools; `--harness none` skips them.
- For the nightly digest: the `claude` CLI, logged in, or `codex` as the fallback.

```bash
git clone https://github.com/Stormbane/smriti.git
cd smriti
pip install -e ".[read]"      # search and embeddings; add codex, witness or dev, e.g. ".[read,codex]"
python scripts/install.py     # memory root, wake files, Claude Code hooks, MCP server, agent doc
smriti index                  # the first run downloads the ONNX embedding model
smriti write "Sam prefers the command first, then the explanation." --branch people/sam
smriti read "how does Sam like answers"
smriti status
```

Then start a new Claude Code session. The briefing appears in the opening context, and `smriti_read`, `smriti_write` and `smriti_status` are among the tools.

**`scripts/install.py`:**
- **Creates the memory root** (`~/.narada` unless `--memory-root` says otherwise; it must be inside your home directory) from [`memory_template/`](memory_template/). It never overwrites existing files.
- **Copies** `wake.py`, `backup.py` and `narada-p.sh` into `<root>/.smriti/`, and links project mirrors for each project it finds under `--projects-root` (default `C:/Projects`).
- **For `--harness claude_code`** (the default), also:
  - adds SessionStart, UserPromptSubmit and PostToolUse hooks to `~/.claude/settings.json`;
  - registers the MCP server in `~/.claude.json`;
  - writes a marked block into `~/.claude/CLAUDE.md`.
- **For `--harness codex`**, does the same in `~/.codex/config.toml` and `~/.codex/AGENTS.md` (needs the `codex` extra).
- **For `--harness none`**, sets up the memory root only.
- **Is idempotent**; run it again after adding projects.

Notes:
- **Install editable.** The installer copies templates from the checkout, and a wheel doesn't ship them.
- **The root is `SMRITI_ROOT`, default `~/.narada`.** If you use another root, pass `--memory-root` to the installer and also set `SMRITI_ROOT` in your user environment. The wake hook carries the root, but the CLI and the MCP server read only the environment.
- **An existing `~/.claude/CLAUDE.md` without smriti's markers** makes the installer stop before changing anything. Run it once with `--migrate-agent-doc` to convert the file; a `.pre-migrate.bak` copy is kept.
- **The installer also wires the PostToolUse ambient-recall hook.** It is being retired (see Status and limits); remove the `associative_recall.py` entry from `~/.claude/settings.json` if you don't want it.
- **The qmd recall daemon.** The installer tries to start it unless `--skip-recall-daemon` is given, and prints a hint when qmd isn't installed. You don't need qmd.

**The nightly cycle (optional):**

```bash
smriti logd --once            # one capture pass, to check the sources
smriti nightly                # one run by hand
smriti tasks install          # schedule logd, nightly and morning
```

The witness step needs `pip install -e ".[witness]"` and a memory root that is a git repository; without them the step fails visibly in the status file. The morning message needs a `notify_cmd` in `daylog.json` (or a sender script at `.smriti/hooks/notify_suti.py`).

**Per project:** run `python <path-to-smriti>/scripts/setup_project.py` from the project's directory (or pass the project path as an argument). It copies the `.ai/` template and links the project's mirror. [`docs/INSTALL.md`](docs/INSTALL.md) has the details, and [`docs/USAGE.md`](docs/USAGE.md) covers use from Python.

## Interfaces

<!-- doc-facts:begin (generated by scripts/doc_facts.py; do not edit by hand) -->
- **CLI:** 19 `smriti` subcommands: `daemon`, `doctor`, `eval`, `index`, `ingest`, `logd`, `merge-concepts`, `metrics`, `morning`, `nightly`, `queue`, `read`, `recall`, `sleep`, `status`, `tasks`, `watch`, `witness`, `write`. Nested: `daemon` has `start`, `status`; `recall` has `query`, `status`, `stats`, `index`, `daemon`.
- **MCP:** 13 tools: `smriti_read`, `smriti_status`, `smriti_write`, `tell_suti`, `smriti_ask_suti`, `smriti_add_todo`, `smriti_propose_goal_shift`, `smriti_record_finding`, `smriti_record_decision`, `smriti_record_feature`, `smriti_update_feature_status`, `smriti_write_handoff`, `smriti_resolve_outbox`.
- **Tests:** 519 test functions in 44 files under `tests/` (pytest collects more, because some are parametrised).
<!-- doc-facts:end -->

**CLI.** `smriti --help` lists the commands; each takes `--help`.

| Group | Commands |
|---|---|
| Core | `smriti write` (`--branch`, `--title`, `--source`), `smriti read` (`-n`, `--no-rerank`), `smriti index` (incremental; `--full` deletes the live chunks before re-embedding them), `smriti status` (index and capture health), `smriti doctor` (checks a Claude Code or Codex bridge without changing anything) |
| Nightly cycle | `smriti logd` (`--once`, `--ensure`), `smriti nightly`, `smriti morning` (`--dry-run`), `smriti tasks` (`install`, `remove`, `status`), `smriti witness` (`stamp`, `verify`) |
| Diagnostics | `smriti metrics` (summary of `.smriti/metrics.jsonl`), `smriti eval` (evaluation cases, being rewritten; an older version wrote into the live tree, so point `SMRITI_ROOT` at a scratch tree) |
| Legacy | `smriti recall` (`query`, `status` and `stats` for the ambient-recall backends; `index` and `daemon` drive qmd) |
| Parked | `smriti sleep` (refuses without `--deep`), `smriti queue`, `smriti daemon`, `smriti watch`, `smriti ingest`, `smriti merge-concepts` |

**MCP tools** (`python -m smriti.mcp_server`, JSON-RPC over stdio, no dependencies):

| Group | Tools |
|---|---|
| Memory | `smriti_read`, `smriti_write`, `smriti_status` |
| Project workflow (writes under `mirrors/<project>/`; not indexed) | `smriti_add_todo`, `smriti_record_finding`, `smriti_record_decision`, `smriti_record_feature`, `smriti_update_feature_status` |
| Session handoff | `smriti_write_handoff` (writes `.smriti/handoff-pending.md`; the next wake shows it once, then archives it) |
| Reference setup only (being moved to a profile, plan 6.3) | `tell_suti` (sends a real message through `notify_cmd`), `smriti_ask_suti`, `smriti_propose_goal_shift` (both write to `outbox/`; never applied automatically), `smriti_resolve_outbox` |

**Hooks:**
- Claude Code: SessionStart runs `wake.py`; UserPromptSubmit touches `.smriti/last-activity`; PostToolUse on `Read|Edit|Write` runs ambient recall (legacy).
- Codex: SessionStart runs `wake.py` with JSON framing; PostToolUse on `Edit|Write|apply_patch` runs ambient recall (legacy).

## Status and limits

- **One setup, being generalised.** smriti grew inside one person's setup with one agent. Personal coupling is still in the code: the user-specific MCP tools above, the wake's person and cross-channel sections, the morning persona, Windows paths in project discovery, and the UTC+10 default. Plan Phase 6 extracts these into a profile and configuration.
- **Parked:** the April 2026 design's deep pipeline (EXTRACT → JUDGE → CROSSLINK, the cognitive cascade, ingest, consolidation, the task queue and its daemon). It is reachable only through `smriti sleep --deep` and the parked commands above, and is slated for removal (plan 4.0). When it ran, it never changed a belief or identity file; it lengthened the pages it touched by 88% and destroyed one source file. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) describes that design.
- **Legacy:** qmd as a recall backend; the Hermes harness (`--harness hermes`); the PreCompact staging hook; the file-touch ambient-recall hook. The last is off in the reference setup, because research found that 4-11% of its injections helped (plan 2.4).
- **Retrieval quality** on the reference tree is moderate: 0.67 hit@5 on the MCP path, 0.81 with reranking. The plan targets 0.95.
- **Not yet true** (see [`docs/SELF.md`](docs/SELF.md)):
  - search results carry no date or provenance;
  - capture records no gaps;
  - the identity summary is loaded without being accepted, and goes stale.
- **Data flow:** the tree and the index are local files. The nightly digest sends each day's conversation log (up to 60,000 characters) to the configured LLM CLI. Backups push to your git remote, the witness sends a hash to public timestamp calendars, and the morning message uses web search. The optional private store keeps its encryption key in plain text in `private/README.md`; the index skips `private/`. See [`docs/PRIVACY.md`](docs/PRIVACY.md).
- **Engineering:** there is no CI. `ruff` and `mypy --strict` don't pass across the tree yet. Linux and macOS are untested.

The plan, with measurements, is [`.ai/features/smriti-renewal.md`](.ai/features/smriti-renewal.md).

## Licence

MIT; see [`LICENSE`](LICENSE). `src/smriti/_vendored/memsearch/` is vendored from zilliztech/memsearch under its MIT licence; see [`NOTICE.md`](NOTICE.md).

## Background

The name and the shape of the design come from Indian theories of mind. The antaḥkaraṇa (manas, buddhi, ahaṃkāra, citta) is used to say which function each component serves, without claiming the component realises it; [`docs/SELF.md`](docs/SELF.md) gives the mapping honestly. The parked April design drew on Karpathy's LLM Wiki pattern, Hindsight's consolidation prompts, Memvid and OpenClaw.

Related project: [svapna](https://github.com/Stormbane/svapna), the dreaming and training side: synthetic data and LoRA updates for a local model that shares the same memory tree.

Built by Suti, with Narada, the AI entity it was built for and the first one to use it.

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
  (vectors from the embedding service)        smriti embed serve  (started on demand,
                                                one model per memory root; exits when idle)

03:00  smriti nightly
       reconcile -> render -> digest -> rollups -> index -> witness -> status
07:00  smriti morning
```

**In a session:**
- **Write.** `smriti_write(content, branch="journal", title?, source?)` appends a dated entry with frontmatter (`date`, `time`, `branch`, `source`) to `<branch>/YYYY/MM-DD.md`, or `journal/YYYY/MM/weekN/MM-DD.md` for the journal. The file is indexed at once, and each existing `index.md` in its parent directories is regenerated to list it (`smriti.store.index_pages`). When no vectors can be made or stored (the embedding service is down or late, or sqlite-vec isn't installed), the entry is still indexed for keyword search at once, and the reply says "saved; indexed for keyword search; vectors pending (reason)"; the vectors arrive later without anyone asking.
- **Read.** `smriti_read(query, top_k=5)` runs hybrid search over the tree. A result's score is 0.5 vector similarity (sqlite-vec) + 0.3 keyword (FTS5) + 0.2 trunk distance, the last favouring files near the root. `smriti read` on the CLI also reranks with a cross-encoder; the MCP tool doesn't. A search that couldn't use vectors runs by keyword only and starts with "keyword-only: reason".
- **Wake.** A SessionStart hook runs `<root>/.smriti/wake.py`, which prints a briefing of at most 9,500 characters. [`docs/SELF.md`](docs/SELF.md) lists its sections and where each comes from.

**The embedding service.** One process per memory root holds the embedding model, and every session's MCP server (and `smriti read`) asks it for vectors, so a server stays small (44 MB measured after searches and writes) instead of loading its own copy of the model (about 1 GB each; 66 servers once held 32 GB). The first search or write that finds it not running starts `python -m smriti embed serve` in a hidden console and waits, bounded: 3 s for a query, 30 s for a write. It exits after 15 minutes without a request (`SMRITI_EMBED_IDLE_S`), which gives the model's memory back, and drains and exits when the index moves to another embedding model, so the next client starts a fresh one. Between requests it fills in the vectors of anything written while it was unavailable. It listens on a loopback port only, and both sides prove they hold a per-user key before anything else is sent ([`SECURITY.md`](SECURITY.md#the-embedding-service)). `smriti embed status` and `smriti status` show its state and how many sources' vectors are pending; `smriti embed stop` drains it now. The nightly's `smriti index` and `smriti eval` load the model in their own short-lived process instead. `SMRITI_EMBED_SERVICE=0` turns the service off: every process then loads the model itself, as before.

**The day-log.** `smriti logd` watches transcript files and merges every turn into one chronological file per local day. Each turn gets a content-derived id, a speaker role (`user` or `agent`), a channel and a time, and credential-shaped strings are masked. Sources are globs in `<root>/.smriti/daylog.json`. The defaults are Claude Code, Codex and a voice-transcript folder. The same file names the two parties (`user_name`, `agent_name`) for the render and the prompts; see [Configuration](#configuration).

**The nightly cycle** (`src/smriti/daylog/nightly.py`). Every step derives from hashed inputs, so a rerun is a no-op. A failed step is recorded in the status file, and the steps after it still run.

| Step | What it does | Writes |
|---|---|---|
| reconcile | Re-scans every source for turns `logd` missed | `log/YYYY/MM/DD.jsonl` |
| render | Deterministic markdown of every day whose input changed | `log/YYYY/MM/DD.md` (not indexed) |
| digest | One LLM call per day: highlights, decisions, open threads. A day log over 60,000 characters goes in up to 4 time-ordered parts of 60,000, one call each, then a merge call that also gets up to 8,000 characters of the autonomous-session markers; anything past those budgets is cut, and the digest's frontmatter records it (`parts`, `chars_seen`, `chars_total`, `cut`, `cut_from`, `markers_seen`, `markers_total`). `claude -p` first (no tools or MCP, isolated from the user's Claude settings), then `codex exec` as a fallback (read-only sandbox; not yet isolated from the user's Codex config). At most 10 days a night, newest first | `log/YYYY/MM/DD-digest.md` |
| rollups | Week, month and year summaries of the digests; at most 2 a night. Nothing reads them yet except search | `log/YYYY/MM/weekN.md`, `log/YYYY/MM.md`, `log/YYYY.md` |
| index | Incremental `smriti index`, retried up to 3 times | `.smriti/index.db` |
| witness | Commits the tree through `backup.py`, timestamps a manifest naming HEAD with OpenTimestamps, pushes | `witness/YYYY/<night>.txt` and `.ots` |
| status | One status document, read by the wake's "while you slept" line | `log/.nightly-status.json` |

**The morning message** (07:00, **experimental**): one message per local day, composed by an LLM from the digest plus a web search for headlines, and sent through the `notify_cmd` configured in `daylog.json`. The ledger entry is written before the send, so a crash means no message, never two. It is written from the configured agent to the configured user, with no persona; a profile can supply its own prompt (the reference profile's carries its agent's persona).

**Backups.** `<root>/.smriti/backup.py` commits the memory tree once an hour when it has changed (`smriti backup --push`, run by the `smriti-backup` task) and pushes only when there is something new; the nightly witness step commits and pushes too. Sessions don't trigger backups. It never kills git, clears stale locks only when no git process is running, and logs to `.git/smriti-backup.jsonl`. You create the git repository and its remote yourself. The tree is pushed in plaintext unless git-crypt encrypts it; the installer asks whether the remote is private and says whether git-crypt covers the day-log, but doesn't set it up (see [Privacy](#privacy)).

**Scheduling.** `smriti tasks install` creates four Windows scheduled tasks: `smriti logd --ensure` every 10 minutes, `smriti backup --push --tag hourly` at 17 minutes past each hour, `smriti nightly` at 03:00, and `smriti morning` at 07:00. On Linux and macOS it prints the equivalent crontab lines instead. Day boundaries follow the system's time zone, daylight saving included; set `SMRITI_DAY_UTC_OFFSET` (hours) or `"timezone"` in `.smriti/daylog.json` (hours, `local`, or an IANA name where Python can load one) to override it.

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
- the identity summary at the top has no date stamp. Nothing rebuilds it automatically; it changes only when someone edits it, so it can be months older than the trunk files it summarises. When it matters, read the trunk files from the reading list at the end of the briefing.

A handoff written with `smriti_write_handoff` is shown at the top of the next session's wake, once, then archived.

**Identity files are edited deliberately.** `identity.md`, `mind/`, `people/` and `open-threads/` change by your own edits, in session, and only when something has really shifted. Say why in a journal entry. No live batch job edits them.

## How the self works

The wake briefing is the *working self*: the part of memory present without a tool call. The *self files* sit at the trunk of the tree: `identity.md`, `mind/` (beliefs, values, desires, practices), `people/`, `open-threads/`, and the dated `journal/`. Today they change by in-session edits, `smriti_write`, and an end-of-session reflection practice. A protocol with explicit layers, evidence requirements and cooling periods is planned and not built. [`docs/SELF.md`](docs/SELF.md) describes each wake section with its source, budget and freshness, and the self files. It also covers the planned self-change protocol, the antaḥkaraṇa (Vedantic model of mind) mapping with what is built and what is design intent, and six invariants, three of which don't hold yet.

## Privacy

**You choose at install whether your conversations are captured.** Capture copies every message you type and every reply the agent writes, from Claude Code transcripts, Codex rollouts and voice transcripts, into `<root>/log/`. The backup commits that with the rest of the memory tree and pushes it to the tree's git remote, in plaintext unless you set up git-crypt. Only a scrubber that masks strings shaped like credentials stands in between. So `scripts/install.py` asks two questions before it changes anything:

1. **Capture conversations into the day-log?** No sets `"sources": []` in `<root>/.smriti/daylog.json`, keeping the file's other keys.
2. **Is the backup remote private?** Asked only when the tree has a remote named `origin`. smriti doesn't set up git-crypt; when the remote isn't confirmed private, the installer prints the steps.

Flags answer them without a terminal: `--capture` or `--no-capture`, and `--remote-is-private` or `--remote-not-private`. With neither a terminal nor a flag, a new memory root captures nothing: capture can be turned on later, and the nightly then reads the transcripts still on disk, but a push can't be taken back. An existing root keeps the sources its `daylog.json` has. The answers are recorded in `<root>/.smriti/privacy.json`, so a rerun doesn't ask again, and the installer's last lines say what is captured, what is committed and where it is pushed. [`docs/INSTALL.md`](docs/INSTALL.md#privacy-capture-and-the-backup-remote) has the details.

**What leaves the machine:**
- **Backups** push everything git tracks in the tree: the memory notes, the captured conversations and their renders, the digests and rollups, and `daylog.json`. The template's `.gitignore` keeps out the index, metrics, runtime state and the private store's key. Even with git-crypt, file names, sizes and commit messages stay readable. [`docs/PRIVACY.md`](docs/PRIVACY.md#what-is-pushed-in-the-clear) lists it all.
- **The nightly digest** sends each day's conversation log (up to 248,000 characters: four parts of 60,000 and 8,000 of autonomous-session markers) to the configured LLM CLI.
- **The witness** sends a hash to public timestamp calendars, and **the morning message** uses web search.
- **The optional private store** encrypts its files; its key stays in `private/.key`, which git ignores, and the index skips `private/`.

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
python scripts/install.py     # two privacy questions, then memory root, wake files, hooks, MCP server, agent doc
smriti index                  # the first run downloads the ONNX embedding model
smriti write "Sam prefers the command first, then the explanation." --branch people/sam
smriti read "how does Sam like answers"
smriti status
```

Then start a new Claude Code session. The briefing appears in the opening context, and `smriti_read`, `smriti_write` and `smriti_status` are among the tools.

**`scripts/install.py`:**
- **Asks the two [privacy](#privacy) questions** first, or takes the answers from `--capture`/`--no-capture` and `--remote-is-private`/`--remote-not-private`, and ends by saying what is captured, committed and pushed.
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
smriti tasks install          # schedule logd, the hourly backup, nightly and morning
```

The witness step needs `pip install -e ".[witness]"` and a memory root that is a git repository; without them the step fails visibly in the status file. The morning message needs a `notify_cmd` in `daylog.json`.

**Per project:** run `python <path-to-smriti>/scripts/setup_project.py` from the project's directory (or pass the project path as an argument). It copies the `.ai/` template and links the project's mirror. [`docs/INSTALL.md`](docs/INSTALL.md) has the details, and [`docs/USAGE.md`](docs/USAGE.md) covers use from Python.

### Configuration

`<root>/.smriti/daylog.json` is optional; every key has a default. `user_name` and `agent_name` (default `"User"` and `"Agent"`) are the names the day render, the nightly and morning prompts and the core tools use for the two speaker roles. `profile` enables an optional profile, `smriti.profiles.<name>`: one installation's own tools, wake sections and morning persona. With none set, smriti runs its generic core. The repo's one profile, `reference`, is the maintainer's installation, which sets `{"profile": "reference", "user_name": "Suti", "agent_name": "Narada"}`. [`docs/INSTALL.md`](docs/INSTALL.md#configuration-rootsmritidaylogjson) lists every key.

The embedding service reads three environment variables: `SMRITI_EMBED_SERVICE=0` turns it off; `SMRITI_EMBED_IDLE_S` sets its idle exit (default 900 seconds); `SMRITI_RUNTIME_DIR` moves its runtime directory (default `%LOCALAPPDATA%\smriti\run` on Windows, `$XDG_RUNTIME_DIR/smriti` or `~/.cache/smriti/run` elsewhere), which holds its key, lock, discovery file and log and is never inside the memory root.

## Interfaces

<!-- doc-facts:begin (generated by scripts/doc_facts.py; do not edit by hand) -->
- **CLI:** 15 `smriti` subcommands: `backup`, `doctor`, `embed`, `eval`, `index`, `logd`, `metrics`, `morning`, `nightly`, `read`, `recall`, `status`, `tasks`, `witness`, `write`. Nested: `embed` has `serve`, `status`, `stop`; `recall` has `query`, `status`, `stats`, `index`, `daemon`.
- **MCP:** 9 core tools: `smriti_read`, `smriti_write`, `smriti_status`, `smriti_write_handoff`, `smriti_add_todo`, `smriti_record_finding`, `smriti_record_decision`, `smriti_record_feature`, `smriti_update_feature_status`.
- **MCP, `reference` profile:** 4 more tools when it is enabled: `tell_suti`, `smriti_ask_suti`, `smriti_propose_goal_shift`, `smriti_resolve_outbox`.
- **Tests:** 659 test functions in 52 files under `tests/` (pytest collects more, because some are parametrised).
<!-- doc-facts:end -->

**CLI.** `smriti --help` lists the commands; each takes `--help`.

| Group | Commands |
|---|---|
| Core | `smriti write` (`--branch`, `--title`, `--source`), `smriti read` (`-n`, `--no-rerank`), `smriti index` (incremental; `--full` deletes the live chunks before re-embedding them), `smriti status` (index and capture health), `smriti doctor` (checks a Claude Code or Codex bridge without changing anything) |
| Nightly cycle | `smriti logd` (`--once`, `--ensure`), `smriti nightly`, `smriti morning` (`--dry-run`), `smriti tasks` (`install`, `remove`, `status`), `smriti witness` (`stamp`, `verify`) |
| Embedding service | `smriti embed serve` (`--idle`; clients start it themselves), `smriti embed status`, `smriti embed stop` (each takes `--root`) |
| Diagnostics | `smriti metrics` (summary of `.smriti/metrics.jsonl`), `smriti eval` (the search cases, run against a synthetic fixture in a disposable sandbox; `--json`, `--baseline`) |
| Legacy | `smriti recall` (`query`, `status` and `stats` for the ambient-recall backends; `index` and `daemon` drive qmd) |

**MCP tools** (`python -m smriti.mcp_server`, JSON-RPC over stdio, no dependencies):

| Group | Tools |
|---|---|
| Memory | `smriti_read`, `smriti_write`, `smriti_status` |
| Project workflow (writes under `mirrors/<project>/`; not indexed) | `smriti_add_todo`, `smriti_record_finding`, `smriti_record_decision`, `smriti_record_feature`, `smriti_update_feature_status` |
| Session handoff | `smriti_write_handoff` (writes `.smriti/handoff-pending.md`; the next wake shows it once, then archives it) |
| The `reference` profile only (listed when `daylog.json` sets `"profile": "reference"`) | `tell_suti` (sends a real message through `notify_cmd`), `smriti_ask_suti`, `smriti_propose_goal_shift` (both write to `outbox/`; never applied automatically), `smriti_resolve_outbox` |

**Hooks:**
- Claude Code: SessionStart runs `wake.py`; UserPromptSubmit touches `.smriti/last-activity`; PostToolUse on `Read|Edit|Write` runs ambient recall (legacy).
- Codex: SessionStart runs `wake.py` with JSON framing; PostToolUse on `Edit|Write|apply_patch` runs ambient recall (legacy).

## Status and limits

- **One setup, being generalised.** smriti grew inside one person's setup with one agent. That setup's MCP tools, wake sections and morning persona are now the optional `reference` profile, and the core names the two parties from configuration. Still coupled: Windows paths in project discovery and the default root `~/.narada`.
- **Removed:** the April 2026 design's deep pipeline (EXTRACT → JUDGE → CROSSLINK, the cognitive cascade, ingest, consolidation, the task queue and its daemon, and the `sleep`, `queue`, `daemon`, `watch`, `ingest` and `merge-concepts` commands) was removed in November 2026; it remains in the git history. When it ran, it never changed a belief or identity file; it lengthened the pages it touched by 88% and destroyed one source file. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) describes that design.
- **Legacy:** qmd as a recall backend; the Hermes harness (`--harness hermes`); the PreCompact staging hook; the file-touch ambient-recall hook. The last is off in the reference setup, because research found that 4-11% of its injections helped (plan 2.4).
- **Retrieval quality** on the reference tree is moderate: 0.67 hit@5 on the MCP path, 0.81 with reranking. The plan targets 0.95.
- **Not yet true** (see [`docs/SELF.md`](docs/SELF.md)):
  - search results carry no date or provenance;
  - capture records no gaps;
  - the identity summary is loaded without being accepted, and goes stale.
- **Data flow:** the tree and the index are local files; [Privacy](#privacy) says what is captured, what backups push, and what the nightly sends to an LLM.
- **Engineering:** CI runs `ruff`, `mypy --strict` and the tests on Windows, Linux and macOS with Python 3.11 and 3.12, with a deterministic stub embedder, so no model is downloaded. Strict types are enforced on the whole package (the vendored code excepted). Day-to-day use is on Windows; on macOS the CI's Python can't load sqlite-vec, so search there is keyword-only.

The plan, with measurements, is [`.ai/features/smriti-renewal.md`](.ai/features/smriti-renewal.md).

## Licence

MIT; see [`LICENSE`](LICENSE). `src/smriti/_vendored/memsearch/` is vendored from zilliztech/memsearch under its MIT licence; see [`NOTICE.md`](NOTICE.md).

## Background

The name and the shape of the design come from Indian theories of mind. The antaḥkaraṇa (manas, buddhi, ahaṃkāra, citta) is used to say which function each component serves, without claiming the component realises it; [`docs/SELF.md`](docs/SELF.md) gives the mapping honestly. The April design, since removed, drew on Karpathy's LLM Wiki pattern, Hindsight's consolidation prompts, Memvid and OpenClaw.

Related project: [svapna](https://github.com/Stormbane/svapna), the dreaming and training side: synthetic data and LoRA updates for a local model that shares the same memory tree.

Built by Suti, with Narada, the AI entity it was built for and the first one to use it.

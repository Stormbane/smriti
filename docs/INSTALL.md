# smriti — Installation

This document covers:

1. [smriti package + Narada session-start system](#1-smriti--entity-session-start) — the main install
2. [New project setup](#2-new-project-setup) — template and mirror wiring for new projects
3. [PreCompact capture hook](#3-precompact-capture-hook) — the raw-turn backstop

For using smriti as a Python library outside the supported harnesses (custom
agents, Cursor / Cline / Continue.dev, evaluation harnesses), see
[USAGE.md](USAGE.md).

---

## 1. smriti + entity session-start

### What this gives you

On every new Claude Code session in any project, the entity (Narada is
the reference entity; the system is not Narada-specific — point it at
any `~/.<entity>/` root) wakes up with:

- A briefing of at most 9,500 characters: the identity summary, last
  night's status, the current project's `STATUS.md`/`INDEX.md` (or
  `todo.md`), recent journal entries, and a reading list of the full
  identity files, plus any sections an enabled profile adds (the
  reference profile's: what the session can reach, the person, the other
  channels). [SELF.md](SELF.md) lists every section with its source and
  budget.
- `smriti_read` / `smriti_write` / `smriti_status` as first-class MCP
  tools in every session

The transport is a `SessionStart` hook that runs the installed
package's wake, `<python> -P -m smriti.wake`; the core sections are fixed
in `src/smriti/wake/briefing.py`. Nothing in the memory tree is run, and
`-P` keeps the current directory (which may be the tree) off the import
path. The hook names the python that ran the installer, by its absolute
path, as the MCP registration does: a bare `python` is whichever one the
harness's PATH finds first, which may not have smriti. Installs before
hardening H15.1 wrote a bare `python`, and before H15.8 ran
`<root>/.smriti/wake.py`, a copy in the tree; rerun the installer to bring
the hook up to date in place (`smriti doctor` names the gap). If the
python can't import smriti, the briefing is missing and it says so in one
line on stderr, exiting 1: Claude Code shows that line as a hook error and
Codex reports a hook failure, and neither stops the session. Per-project
access is via directory junctions under `<entity>/mirrors/{project}/`.
The memory search tool is registered as an MCP server in `~/.claude.json`.

**Wake is silent unless `SMRITI_WAKE=1` is set** in its environment — the
SessionStart hook sets this so interactive sessions wake fully, while
`claude -p` and other headless callers (such as the nightly digest) stay
clean. To inject the entity's identity into a one-shot
headless call anyway, use `<entity>/.smriti/narada-p.sh "your prompt"` —
it prepends the wake output (`python -P -m smriti.wake`, with the `python`
on your PATH) and forwards to `claude -p`.

### Prerequisites

- Windows 10/11 is the most-tested platform. On Linux/macOS, mirrors are
  symlinks and `smriti tasks install` only prints crontab lines; CI installs
  the wheel and runs it on Ubuntu, but no install there runs day to day.
- Python 3.11+ on PATH, and git
- Claude Code (or Codex, with `--harness codex`)
- Optionally, an existing memory root restored from backup; otherwise the
  installer creates one from `memory_template/`

### Install steps

```bash
# 1. Install the package: from the repository (the name `smriti` on PyPI is
#    another project), or a release's wheel. The wheel carries the memory
#    template and the scripts the installer deploys.
pip install "smriti[read] @ git+https://github.com/Stormbane/smriti.git"
#    Or from a checkout, to work on smriti (then `python scripts/install.py`
#    runs the same installer as `python -m smriti.install` below):
#    git clone https://github.com/Stormbane/smriti.git && cd smriti
#    pip install -e ".[read]"      # add ,codex / ,witness / ,dev as needed

# 2. The memory root. Default: ~/.smriti/ (env SMRITI_ROOT), or ~/.narada/
#    where that already exists (the default before 0.2); it must be
#    inside your home directory. `smriti status` shows which root is used. On a fresh machine with an existing tree,
#    restore it from backup first; otherwise the installer creates it from
#    memory_template/.

# 3. Run the installer: asks whether to capture conversations and whether
#    the backup remote is private (see "Privacy" below), then copies the
#    memory template (never overwriting),
#    copies narada-p.sh into <root>/.smriti/ (and removes the wake.py
#    and backup.py an earlier install put there),
#    creates mirrors/ junctions, registers the smriti MCP server,
#    patches ~/.claude/settings.json, writes the managed block in
#    ~/.claude/CLAUDE.md. --projects-root names the directory whose
#    projects get mirrors; it is recorded in <root>/.smriti/install.json
#    and reused by every later run (a new install links none without it).
python -m smriti.install --projects-root ~/code
# or for a different entity (then also set SMRITI_ROOT in your user
# environment: the CLI and the MCP server read only the environment):
python -m smriti.install --memory-root ~/.ari

# 4. Build the index and check the round trip.
smriti index
smriti write "Sam prefers the command first, then the explanation." --branch people/sam
smriti read "how does Sam like answers"

# 5. Start a new Claude Code session in any project to verify. The
#    briefing should be in the opening context, and `smriti_read` should
#    appear in its tool list.
```

The installer is idempotent — re-run any time after adding a new project
or after the smriti repo's `narada/` templates change. An install made
before the projects root was recorded keeps the directory its mirrors
point into (or `C:/Projects`, the old default, when it has no mirrors and
that directory exists), and records it.

### Privacy: capture and the backup remote

Once capture is on, `smriti logd` copies every message you type and
every reply the agent writes, from Claude Code transcripts, Codex rollouts
and voice transcripts, into `<root>/log/`, masking only strings shaped
like credentials. The backup commits the tree, `log/` included, and pushes
the current branch to its upstream remote (`origin` when the branch has no
upstream; nothing on a detached HEAD) in plaintext unless git-crypt
encrypts it. Capture is therefore off until `<root>/.smriti/daylog.json`
turns it on: with no such file, or one with neither `"capture": true` nor
a `"sources"` list, nothing is captured, and `smriti logd` (at start and
whenever the file changes) and `smriti status` say why and how to turn it
on. Until hardening H11.1 such an install captured every transcript on
the machine. The installer asks two questions before it changes anything:

1. **"Capture conversations into the day-log?"** Yes sets
   `"capture": true` in `<root>/.smriti/daylog.json` (replacing a
   `false`), which turns on the default sources, and removes an empty
   `"sources": []`; a list of your own sources is left alone, and a
   `"capture": false` beside it is removed. No sets `"capture": false`,
   which captures nothing, `extra_sources` included, and `"sources": []`
   (which an older `smriti logd` still running also reads as nothing).
   Only those keys change: the file's other keys are kept, `extra_sources`
   too, and it applies again when capture is turned back on. A running
   `smriti logd` rereads `daylog.json` when it changes, so the choice
   reaches it on its next pass, within a few seconds.
2. **"Is `<url>` a private repository that only you can read?"** Asked
   only when the memory root is a git repository whose backup remote (the
   branch's upstream remote, or `origin`) exists and HEAD is on a branch;
   otherwise the installer says why nothing is pushed and skips it. When
   that remote has several push URLs, `git push` pushes to each, so the
   question names them all: "Are `<a>` and `<b>` all private
   repositories that only you can read?" Each URL is the one git really
   pushes to, after its `url.<base>.pushInsteadOf` and `insteadOf`
   rewrites, so changing a rewrite asks again. smriti never sets up git-crypt.
   When the answer is no, or nobody answered, the installer prints the
   steps; [PRIVACY.md](PRIVACY.md#what-is-pushed-in-the-clear) has them
   too.

| Flag | Answers |
|---|---|
| `--capture` | 1, yes |
| `--no-capture` | 1, no |
| `--remote-is-private` | 2, yes |
| `--remote-not-private` | 2, no |

```bash
python scripts/install.py --no-capture --remote-is-private
python scripts/install.py --harness none --capture --remote-not-private
```

How each question is settled, in order:

| | Capture | Remote |
|---|---|---|
| A flag | The flag | The flag (ignored, with a note, when there is no remote) |
| Answered before, by a flag or at a prompt | Kept; not asked again | Kept, while the backup pushes to the same remote with the same push URLs; another upstream remote, or adding, removing or changing a URL, asks again |
| A terminal (stdin and stdout) | Asked. A new root needs `y` or `n`; on an existing one, Enter keeps what `daylog.json` does now | Asked; needs `y` or `n` |
| No terminal, no flag, new root | **Off**: `"capture": false`, with a note | Not confirmed private: the summary says so and prints the git-crypt steps |
| No terminal, no flag, existing root | `daylog.json` is left as it is, with a note saying whether it captures | The same |

An existing root is one with `.smriti/daylog.json`, `.smriti/install.json`
(every install records itself there), the `.smriti/wake.py` or `backup.py`
an install from before that record left, or `log/`. Without a terminal,
capture stays off for a new root because it can be turned on later
(re-run with `--capture`; the nightly then reads the transcripts still on
disk) while a push can't be taken back. A
"yes" recorded before H11.1, whose `daylog.json` says neither `"capture"`
nor `"sources"` (or that has none), relied on the old default; a rerun
writes `"capture": true` for it, with a note, and asks nothing. A
`daylog.json` that isn't a JSON object makes the installer stop before
changing anything: capture would otherwise ignore it and capture
nothing.

The answers are recorded in `<root>/.smriti/privacy.json` (when, how,
and the remote's push URLs without credentials), and the installer's last
lines say what is captured, what is committed, and where and how it is
pushed. For that it asks git about every kind of file under `log/` that
holds a conversation or a summary of one: `DD.jsonl`, its `DD.md` render,
`DD-digest.md`, and the `weekN.md`, `MM.md` and `YYYY.md` rollups. The
day-log counts as kept out of git only when every kind is ignored and no
such file is already tracked or waiting on disk. An ignore rule doesn't
untrack a day committed before it, and each backup's `git add -A` keeps
committing its changes; files capture wrote before the first backup go
with it, even after `--no-capture`. The day-log is reported encrypted
only when every kind, and every such file, is marked `filter=git-crypt`,
the clone has `filter.git-crypt.clean` and `.smudge` configured, and
`git-crypt status` reports each tracked day encrypted. A
`.gitattributes` line alone isn't enough: git passes a filter it has no
configuration for straight through. Without `git-crypt` on PATH the
protection is "unverified", and the summary still warns about plaintext.
The backup and the nightly push only once `smriti tasks install` has
scheduled them.

### Selecting a harness

Claude Code is the backwards-compatible default. The installer can wire the
same memory root into any supported harness:

```bash
python scripts/install.py --harness claude_code
python scripts/install.py --harness codex
python scripts/install.py --harness hermes
python scripts/install.py --harness none  # core memory setup only
```

### What the installer does

| Step | Location | Purpose |
|---|---|---|
| Ask the privacy questions | `<entity>/.smriti/daylog.json` (`sources` only), `<entity>/.smriti/privacy.json` | Capture on or off; whether the backup remote is private ([above](#privacy-capture-and-the-backup-remote)) |
| Copy the memory template | `<entity>/` from `memory_template/` | Empty identity tree (never overwrites existing files) |
| Copy `narada-p.sh` | `<entity>/.smriti/narada-p.sh` | Wake-injecting wrapper for headless `claude -p`, run by hand. A smriti-managed copy is upgraded, keeping a `.pre-upgrade.bak` |
| Remove `backup.py`, `wake.py` | `<entity>/.smriti/` | Copies an earlier install deployed; the wake and the backup run from the installed package now. A smriti-managed copy is removed (the tree's git history keeps it); a hand-written one is left, with a warning. `wake.py` goes at the end of a successful install, and only when no hook in `~/.claude/settings.json` or `~/.codex/config.toml` (nor `narada-p.sh`) still runs it; otherwise it stays, and the installer says which harness to reinstall |
| Ensure `.smriti/` dir | `<entity>/.smriti/` | Smriti runtime state (index, metrics, `last-activity`) |
| Create junctions | `<entity>/mirrors/{project}/auto-memory`, `/knowledge`, `/ai` | Per-project memory and knowledge access, for each project under the projects root |
| Record the projects root | `<entity>/.smriti/install.json` | The next run links the same projects without `--projects-root` |
| Register MCP server | `~/.claude.json` | Exposes `smriti_read` / `smriti_write` / `smriti_status` |
| Patch settings.json | `~/.claude/settings.json` | SessionStart → `python -P -m smriti.wake` (an earlier install's hook is rewritten in place); UserPromptSubmit → touch `last-activity` |
| Write CLAUDE.md | `~/.claude/CLAUDE.md` | Managed block: the memory contract from `src/smriti/templates/AGENT.md` |

Claude Code rewrites `~/.claude.json` and `settings.json` while it runs, so
the installer edits them carefully: it holds a lock from its read to its
write, reads the file again just before writing (if Claude Code changed it
meanwhile, the change is made again on the new text, once; if it changed
again, the file is left alone and the install exits nonzero, to be run
again), writes atomically with the file's permissions, and first copies the
file as it was to `<file>.smriti-<UTC time>.bak`. The first such backup,
the file before smriti changed it, is never removed; five are kept in all.

The table above describes the default Claude Code bridge. The Codex bridge
writes `~/.codex/AGENTS.md`, patches `~/.codex/config.toml`, registers the MCP
server, and installs the wake hook. Verify it without changing state:

```bash
smriti doctor --harness codex --project C:/Projects/your-project
```

The Hermes bridge writes `~/.hermes/SOUL.md`. It is a generated composition of
`<entity>/.smriti/wake-context.md` and
`<entity>/.smriti/context/hermes.md`; do not edit it as the source of truth.
The write is atomic, which safely replaces the old core-only hardlink. Nothing
rebuilds `wake-context.md` automatically; after editing it, rerun
`python scripts/install.py --harness hermes` to refresh the live SOUL.

### Audience-specific wake context

The core wake briefing is deliberately harness-neutral. Optional overlays live
at `<entity>/.smriti/context/<audience>.md`. Claude Code and Codex select the
`coding` audience; Hermes uses `hermes`. Keeping receptionist/orchestration
instructions in `context/hermes.md` prevents them from leaking into coding
sessions while preserving a single shared identity core.

### What the wake loads

The core sections, their order and their budgets are fixed in
`src/smriti/wake/briefing.py`; an enabled profile adds its own (see
[Profiles](#profiles)). [SELF.md](SELF.md) describes each one.
`narada/wake.md` and `<entity>/wake.md` are obsolete: nothing reads them.

### Smriti CLI (optional, grows over time)

```bash
smriti index          # build the search index over the memory root
smriti read "query"   # semantic + keyword search
smriti write "text"   # write an entry to the memory tree
smriti nightly        # sleep task: reconcile day-log, render, digest, rollups, reindex
smriti morning        # wake task: the one good-morning message
smriti logd           # day-log watcher daemon (--ensure = keepalive)
smriti tasks install  # schedule logd keepalive, hourly backup, 03:00 nightly, 07:00 morning
smriti status         # index stats + day-log capture health
```

### The nightly cycle (day-log)

Claude Code transcripts, Codex rollouts and voice transcripts (the default
sources) are merged in real time into one chronological day-log at
`<root>/log/YYYY/MM/DD.jsonl` by `smriti logd`. The 03:00 `smriti
nightly` run renders each day to markdown, writes an LLM digest
(`claude -p`, with `codex exec` as an opt-in fallback — subscription seats only),
builds week/month/year rollups, and refreshes the indexes. The 07:00
`smriti morning` run sends one good-morning message (digest
highlights + world headlines) through the `notify_cmd` configured in
`<root>/.smriti/daylog.json` — at most one per day, fail-closed.
Source globs live in the same config file; new transcript directories are
a config edit, never a code change. See
`.ai/features/nightly-cycle.md` for the full contract.

Each `claude -p` call (a digest, a rollup, the morning message) is given 300
seconds; `SMRITI_CLAUDE_TIMEOUT` sets another limit, in seconds.
`NARADA_CLAUDE_TIMEOUT`, its older name, is still read when the new one isn't
set.

The witness step needs the `witness` extra and a memory root that is a git
repository. The README's [What runs today](../README.md#what-runs-today)
lists every nightly step with what it writes.

### Configuration: `<root>/.smriti/daylog.json`

One small JSON file in the memory root configures capture, the nightly,
the morning and the profile. Every key is optional; without the file the
defaults apply, and capture is off.

| Key | Default | What it does |
|---|---|---|
| `capture` | off | `true` captures from the default sources: Claude Code transcripts (`~/.claude/projects/*/*.jsonl`), Codex rollouts (`~/.codex/sessions/*/*/*/rollout-*.jsonl`) and voice transcripts (`<root>/heartbeat/voice-transcripts/*/*.md`); the installer's `--capture` sets it. `false` captures nothing, whatever `sources` and `extra_sources` list (the installer's `--no-capture`) |
| `sources` | none | Capture globs, `[{"name", "kind", "glob"}]`; replaces the default sources, `capture` or not. `[]` captures none of them (`extra_sources` still applies) |
| `extra_sources` | none | Capture globs added to whichever of the above applies |
| `notify_cmd` | none | The command `smriti morning` pipes its message to; no command, no morning |
| `location` | none | Where the user lives, for the morning's local headlines |
| `morning_web_search` | `false` | `true` lets the morning call search the web for headlines, and only that; off, it gets no tools and writes from the digest alone. Search results can carry a prompt injection: see [PRIVACY.md](PRIVACY.md#what-goes-to-an-llm) |
| `nightly_fallback` | none | The provider the digests, rollups and morning message fall back to when Claude Code fails, e.g. `"codex_cli"`; the morning's fallback writes from the digest alone, without web search. Off by default: codex's read-only sandbox still reads your files while it reads untrusted transcripts (see [PRIVACY.md](PRIVACY.md#what-goes-to-an-llm)). `SMRITI_NIGHTLY_FALLBACK`, when set, wins |
| `user_name` | `"User"` | The user's display name: the day render, the presence line, the nightly and morning prompts, the core tools' text, the reading list's person file (`people/<name>/<name>.md`) |
| `agent_name` | `"Agent"` | The agent's display name, used in the same places |
| `profile` | none | An optional profile: the name an installed package registers in the `smriti.profiles` entry-point group; see below |

The day-log stores each speaker as a role, `user` or `agent`; the names
above are only how they are shown. Logs written before roles existed
hold `"suti"` and `"narada"` and are read as `user` and `agent`, with
their turn ids unchanged.

### Profiles

A profile holds what only makes sense for one installation: MCP tools,
wake-briefing sections, a morning persona. Without one, smriti runs its
generic core: the MCP server lists the core tools only, the briefing has
no profile sections, and the morning message has no persona.

A profile is an installed package, not part of smriti: smriti finds it
through the `smriti.profiles` entry-point group of the Python environment
it runs in, and never loads code from the memory tree, the current
directory or the running script's directory. The lookup is cached in the
runtime directory and follows installs and removals (the import path's
directories and their modification times key it), so a new process
doesn't read every installed package's metadata again. A profile that
fails to load costs only its own tools and sections (the MCP server and
the wake say so); the morning refuses to send rather than drop a persona.

The only profile in the repo is `reference`, the maintainer's own
installation (the agent Narada, the user Suti). It is its own
distribution, `smriti-reference`, in `profiles/reference/`, and smriti's
wheel carries none of it; install it with `pip install -e profiles/reference`. It adds the `tell_suti`,
`smriti_ask_suti`, `smriti_propose_goal_shift` and `smriti_resolve_outbox`
tools, the wake's `HANDS`, `SUTI NOW` and `ACROSS SURFACES` sections, and
the morning persona. That installation enables it, with the morning's web
search and the codex fallback, with:

```json
{"profile": "reference", "user_name": "Suti", "agent_name": "Narada",
 "morning_web_search": true, "nightly_fallback": "codex_cli"}
```

To write your own, make a package whose module holds a
`smriti.profiles.Profile` (every hook is optional), register it in its
`pyproject.toml`, install it beside smriti, and name it in `profile`:

```toml
[project.entry-points."smriti.profiles"]
myprofile = "my_profile:PROFILE"
```

Two installed packages claiming one name is an error, not a choice.

---

## 2. New project setup

When starting a new project that should use smriti for memory, two things
need to happen: the project needs a `.ai/` skeleton with knowledge docs
and a CLAUDE.md, and the entity's memory tree needs mirror junctions so
the wake can find the project on session start.

### The project template

The template lives at `project_template/` in the smriti repo. It contains:

```
project_template/
  CLAUDE.md                       -- project description, commands, rules
  .gitignore                      -- standard ignores
  .claude/settings.local.json     -- git permission allows
  .ai/
    todo.md                       -- project roadmap
    knowledge/
      spec.md                     -- what the project is (fill in first session)
      architecture.md             -- tech stack, structure (fill in first session)
      glossary.md                 -- domain terms (fill in as they emerge)
      conventions.md              -- coding patterns (fill in as they emerge)
      lessons-learned.md          -- project-specific insights
```

Memory persistence is handled entirely by smriti (`smriti_write` /
`smriti_read`). Identity loads from `~/.<entity>/` via the wake hook. There
are no per-project memory files, agent definitions, or session hooks
in the template -- the global smriti install handles all of that.

### Setup a new project

One command from the project directory:

```bash
cd C:/Projects/my-new-project
python C:/Projects/smriti/scripts/setup_project.py
```

This copies the template files (if they don't already exist), ensures
`.claude/` is in `.gitignore`, and creates the mirror junctions. For an
existing project that already has `.ai/` files, it skips what's there
and only adds what's missing.

Use `--no-template` to skip the template copy and only wire mirrors.

### What setup_project.py does

For a project at `C:/Projects/foo`:

**Step 1 -- Template.** Copies template files (CLAUDE.md, `.ai/knowledge/`,
`.ai/todo.md`, `.claude/settings.local.json`) into the project. Files that
already exist are skipped.

**Step 2 -- Gitignore.** Ensures `.claude/` is in `.gitignore`. If no
`.gitignore` exists, copies the template's. If one exists, appends only
the missing entries.

**Step 3 -- Mirrors.** Creates junctions in the entity's memory tree:

```
<root>/mirrors/foo/
  auto-memory/  --> ~/.claude/projects/C--Projects-foo/memory/
  knowledge/    --> C:/Projects/foo/.ai/knowledge/
  ai/           --> C:/Projects/foo/.ai/
```

These junctions let the wake load the project's auto-memory and todo.md
on session start. Stale junctions from old setups are cleaned up
automatically.

### Options

```
python scripts/setup_project.py [project-path] [--memory-root PATH] [--no-template]
```

| Flag | Default | Purpose |
|---|---|---|
| `project-path` | current directory | The project to wire up |
| `--memory-root` | `$SMRITI_ROOT`, else `~/.narada` if it exists, else `~/.smriti` | Entity memory root |
| `--no-template` | off | Skip template copy, only create mirrors |

The script is idempotent -- re-run any time.

---

## 3. PreCompact capture hook

### What it does

Claude Code compacts conversation context when it gets too large. The
compacted summary is lossy — details of earlier turns can be lost
forever. This hook captures the raw user/assistant turns as individual
markdown files *before* the compaction runs, so the raw conversation
is durable.

Every Claude Code session on your machine writes to a single
per-user staging directory. Events are namespaced by project:

```
~/.claude/narada-staging-events/
├── .markers/
│   └── {session_id}.json
├── svapna-narada/
│   └── 2026/04/2026-04-11/
│       ├── 6bd926fa-turn-0001.md
│       └── ...
├── beautiful-tree-narada/
│   └── 2026/04/2026-04-07/
│       └── ...
└── <any-other-project>-narada/
    └── ...
```

Entity names are derived from the working directory: the cwd basename
becomes `{basename}-narada`.

### Install

**1. Copy the hook script** to the user hooks directory:

```bash
cp src/smriti/integrations/claude_code/hooks/precompact_capture.py ~/.claude/hooks/precompact_capture.py
```

**2. Wire it** in `~/.claude/settings.json` under `hooks.PreCompact`:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.claude/hooks/precompact_capture.py"
          }
        ]
      }
    ]
  }
}
```

**3. Verify**: start a session, run `/compact`, then check
`~/.claude/narada-staging-events/{projectname}-narada/` for dated
markdown files.

### What gets captured

Per user/assistant turn, a markdown file with YAML frontmatter:

```markdown
---
session_id: <uuid>
turn_number: <integer>
role: user | assistant
timestamp: <UTC ISO-8601>
captured_at: <UTC ISO-8601>
trigger: manual | auto
entity: <derived-from-cwd>
cwd: <absolute working directory>
---

<the turn content, verbatim>
```

### What is NOT captured

- Thinking blocks (model internal reasoning — private, volatile)
- System messages, permission prompts, file-history snapshots
- Sessions older than the last marker (incremental — only new turns since
  last run)

### Safety properties

- **Never blocks compaction.** Exits 0 on any error.
- **Incremental.** Per-session markers track the last JSONL line processed.
- **Idempotent.** Running with no new turns is a no-op.

### What reads it

Nothing ingests the staging tree automatically. It is a backstop that runs
alongside the day-log, which is the live capture path.

---

---

## Install order, agent-doc migration, and the symlink question

### Order on a fresh machine

Clone/restore the **entity repo first**, then run the harness install:

```
git clone <entity-remote> ~/.smriti     # identity, memory, .smriti/ hooks
pip install -e <smriti-checkout>
python scripts/install.py --harness claude_code   # or codex
```

The entity tree is itself a versioned git repository (committed and
pushed by `smriti backup` from the hourly `smriti-backup` task), so entity-level
hook scripts that live under `<root>/.smriti/hooks/` — e.g. a
personal session-ending reminder — come back with the clone. Harness
configs that reference them assume the clone exists; `smriti doctor`
has a hook-target-exists check that catches the gap.

### Agent-doc managed block

`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` are shared real estate:
smriti owns exactly one block between `<!-- smriti:managed:begin -->`
and `<!-- smriti:managed:end -->` markers and never touches a byte
outside it. A pre-marker (unmarked) doc makes the installer refuse at
preflight — before mutating anything — and exit nonzero. Convert it
once with:

```
python scripts/install.py --harness claude_code --migrate-agent-doc
```

Migration recognizes legacy generated sections by heading, replaces
them with the marked block, preserves everything else byte-for-byte,
and leaves a `.pre-migrate.bak` backup next to the file.

### Why deployed hooks are copies, not symlinks

Deployed hook scripts (`~/.claude/hooks/precompact_capture.py`) are copies
of the sources in `smriti/integrations/`, on purpose:

- True file symlinks on Windows require Developer Mode or elevation
  (`WinError 1314` otherwise), and git-bash `ln -s` silently degrades
  to a copy — a link that looks real but is not.
- Their text rarely changes.
- Byte-compare on deploy plus the `smriti doctor` integrity check make
  drift visible without link semantics.

Revisit only if the fleet ever becomes POSIX-only.

## Questions and issues

smriti is being built in public. Include in issue reports:

- Your OS + Claude Code version
- Project layout (cwd path, naming)
- Whether you are installing for a specific entity (a particular LoRA-tuned
  model) or the generic fudge-layer install

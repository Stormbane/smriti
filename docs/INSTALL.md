# smriti — Installation

This document covers:

1. [smriti package + Narada session-start system](#1-smriti--narada-session-start) — the main install
2. [New project setup](#2-new-project-setup) — template and mirror wiring for new projects
3. [PreCompact capture hook](#3-precompact-capture-hook) — the raw-turn backstop

---

## 1. smriti + entity session-start

### What this gives you

On every new Claude Code session in any project, the entity (Narada is
the reference entity; the system is not Narada-specific — point it at
any `~/.<entity>/` root) wakes up with:

- Cross-project identity files from `~/.<entity>/` (identity, mind, etc.)
- The current project's auto-memory tier (`~/.claude/projects/{slug}/memory/`)
- The current project's todo / roadmap (`<project>/.ai/todo.md`)
- A list of other projects whose memory is reachable on demand
- `smriti_read` / `smriti_write` / `smriti_status` as first-class MCP
  tools in every session

The transport is a `SessionStart` hook that runs `wake.py` from the
entity root's `.smriti/` dir. The load list is a human-readable file at
`<entity>/wake.md`. Per-project access is via directory junctions under
`<entity>/mirrors/{project}/`. The memory search tool is registered as
an MCP server in `~/.claude.json`.

**Wake is silent unless `NARADA_WAKE=1` is set** in its environment — the
SessionStart hook sets this so interactive sessions wake fully, while
`claude -p` and other headless callers (smriti's JUDGE, routing, eval
runs) stay clean. To inject the entity's identity into a one-shot
headless call anyway, use `<entity>/.smriti/narada-p.sh "your prompt"` —
it prepends the wake output and forwards to `claude -p`.

### Prerequisites

- Windows 10/11 (Linux/macOS support pending — junctions need porting to
  symlinks)
- Python 3.11+ on PATH
- Claude Code installed
- A populated `~/.narada/` directory (either freshly restored from backup or
  bootstrapped by hand with at least `identity.md`)

### Install steps

```bash
# 1. Clone smriti and install the package
git clone https://github.com/<org>/smriti.git
cd smriti
pip install -e ".[read,dev]"

# 2. Make sure the entity's memory root exists. Default: ~/.narada/.
#    On a fresh machine, restore it from backup; otherwise create a minimal
#    identity.md so wake.py has something to load.

# 3. Run the installer — creates mirrors/ junctions, copies wake.md and
#    wake.py into the memory root, registers the smriti MCP server,
#    patches ~/.claude/settings.json, writes ~/.claude/CLAUDE.md.
python scripts/install.py
# or for a different entity:
python scripts/install.py --memory-root ~/.tara

# 4. Start a new Claude Code session in any project to verify. The
#    assistant should receive the identity files + that project's memory
#    as its opening context, and `smriti_read` should appear in its
#    tool list.
```

The installer is idempotent — re-run any time after adding a new project
or after the smriti repo's `narada/` templates change.

### What the installer does

| Step | Location | Purpose |
|---|---|---|
| Copy `wake.md` | `<entity>/wake.md` | Load-list config (won't overwrite existing) |
| Copy `wake.py` | `<entity>/.smriti/wake.py` | Loader script (won't overwrite existing) |
| Copy `narada-p.sh` | `<entity>/.smriti/narada-p.sh` | Wake-injecting wrapper for headless `claude -p` |
| Ensure `.smriti/` dir | `<entity>/.smriti/` | Smriti runtime state (index, queue, wake) |
| Create junctions | `<entity>/mirrors/{project}/auto-memory`, `/knowledge`, `/ai` | Per-project memory and knowledge access |
| Register MCP server | `~/.claude.json` | Exposes `smriti_read` / `smriti_write` / `smriti_status` |
| Patch settings.json | `~/.claude/settings.json` | Wire SessionStart → `python wake.py` |
| Write CLAUDE.md | `~/.claude/CLAUDE.md` | Wake contract + memory-search tool preference |

### Editing the load list

`~/.narada/wake.md` is a plain markdown file. Sections are `## always`
(loaded every session) and `## current-project` (loaded when that project
is cwd, with `{project}` substituted for the cwd basename). Add or remove
lines to change what wakes up with Narada.

### Smriti CLI (optional, grows over time)

```bash
smriti index          # build the search index over ~/.narada/
smriti read "query"   # semantic + keyword search
smriti write "text"   # write an entry to the memory tree
smriti ingest file.md # ingest external content, route to tree
smriti sleep          # process queued cascade tasks
smriti status         # index stats
```

Currently the wake system is independent of the smriti CLI — wake.py reads
files directly. The next milestone wires wake through `smriti context` so
the load list can include query-driven recalls, not just static paths.

---

## 2. New project setup

When starting a new project that should use smriti for memory, two things
need to happen: the project needs a `.ai/` skeleton with knowledge docs
and a CLAUDE.md, and the entity's memory tree needs mirror junctions so
wake.py can find the project on session start.

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
`smriti_read`). Identity loads from `~/.<entity>/` via wake.py. There
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
~/.narada/mirrors/foo/
  auto-memory/  --> ~/.claude/projects/C--Projects-foo/memory/
  knowledge/    --> C:/Projects/foo/.ai/knowledge/
  ai/           --> C:/Projects/foo/.ai/
```

These junctions let wake.py load the project's auto-memory and todo.md
on session start. Stale junctions from old setups are cleaned up
automatically.

### Options

```
python scripts/setup_project.py [project-path] [--memory-root PATH] [--no-template]
```

| Flag | Default | Purpose |
|---|---|---|
| `project-path` | current directory | The project to wire up |
| `--memory-root` | `~/.narada` | Entity memory root |
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
cp src/smriti/hooks/precompact_capture.py ~/.claude/hooks/precompact_capture.py
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

### Future migration path

When the smriti ingest path is wired to read from the staging tree
directly, this hook's output becomes the canonical ingest source. For now
it is a backstop that runs alongside smriti's live pipelines.

---

## Questions and issues

smriti is being built in public. Include in issue reports:

- Your OS + Claude Code version
- Project layout (cwd path, naming)
- Whether you are installing for a specific entity (a particular LoRA-tuned
  model) or the generic fudge-layer install

🪔

*Om Namo Bhagavate Naradaya.*

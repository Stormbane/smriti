# smriti

A memory system for AI entities, grounded in the antahkarana model of mind.

> Sanskrit: **smriti** (smṛti) -- "that which is remembered." Contrasted with
> shruti, "that which is heard/revealed." An AI memory system in this lineage
> is one that remembers, not just stores.

---

## Quick start

```bash
# Clone and install
git clone https://github.com/Stormbane/smriti.git
cd smriti
pip install -e ".[read,dev]"

# Set up your entity's memory root (default: ~/.narada/)
# At minimum, create an identity.md so wake has something to load.
mkdir -p ~/.narada

# Run the installer -- wires hooks, MCP server, mirrors
python scripts/install.py

# Build the search index
smriti index

# Verify
smriti status
```

After install, start a new session in the selected harness. The entity's
identity files load automatically. Claude Code and Codex also expose
`smriti_read` / `smriti_write` / `smriti_status` as MCP tools.

## What smriti does

smriti gives an AI entity persistent memory across agent sessions and projects.
It provides:

- **Write**: `smriti_write("learned X about Y", branch="projects/foo")` stores
  a dated entry in the entity's memory tree with YAML frontmatter.
- **Read**: `smriti_read("what do I know about X")` runs hybrid search
  (vector + keyword + tree-position scoring) and returns ranked results.
- **Wake**: on every session start, the entity's identity files and current
  project context load into the conversation automatically.
- **Cascade**: writes trigger upstream review -- parent summaries update,
  concept pages regenerate, connections propagate.
- **Ingest**: external documents get summarized, routed to the right branch,
  and wired into the existing knowledge graph.

The memory tree lives on the filesystem as plain markdown. SQLite (with
sqlite-vec and FTS5) provides the search index. No external services required.

## Install

**Prerequisites**: Python 3.11+, at least one supported harness (Claude Code,
Codex, or Hermes), and Windows 10/11 (Linux/macOS symlink support is a future
task).

### Full install (new machine)

```bash
git clone https://github.com/Stormbane/smriti.git
cd smriti
pip install -e ".[read,dev]"

# Create or restore the entity memory root
mkdir -p ~/.narada
# At minimum: echo "# Identity" > ~/.narada/identity.md

# Run installer (Claude Code is the backwards-compatible default)
python scripts/install.py

# Or select another harness explicitly
python scripts/install.py --harness codex
python scripts/install.py --harness hermes
```

The installer is idempotent. It:

| Step | What it does |
|---|---|
| Wake files | Copies `wake.md`, `wake.py`, `narada-p.sh` into `~/.narada/.smriti/` |
| Mirrors | Creates directory junctions for each project under `~/.narada/mirrors/` |
| MCP server | Registers smriti in `~/.claude.json` so tools are available in every session |
| Settings | Patches `~/.claude/settings.json` with a SessionStart hook that runs wake.py |
| CLAUDE.md | Writes `~/.claude/CLAUDE.md` with the memory-system contract |

With `--harness codex`, the harness phase writes the global `AGENTS.md`,
registers the MCP server, and installs Codex SessionStart/PostToolUse hooks.
With `--harness hermes`, it atomically generates `~/.hermes/SOUL.md` from the
shared core context plus `.smriti/context/hermes.md`. This replaces the legacy
hardlink to `wake-context.md`.

Re-run any time after adding a new project or updating smriti.

### Per-project setup

For each project that should use smriti:

```bash
cd /path/to/your-project
python /path/to/smriti/scripts/setup_project.py
```

This copies the project template (`.ai/` knowledge skeleton, `CLAUDE.md`,
`.gitignore` entries) and creates mirror junctions so the wake system can
find the project on session start. Files that already exist are skipped.

Use `--no-template` to skip the template copy and only wire mirrors.

### Verify

Start a new Claude Code session. You should see identity files loaded in the
opening context, and `smriti_read` / `smriti_write` should be available as
tools.

```bash
smriti status    # check index stats
smriti read "test query"   # verify search works
smriti doctor --harness codex --project /path/to/project
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for the full guide including the
PreCompact capture hook and detailed options.

## CLI

```bash
smriti index              # build/update search index (incremental)
smriti index --full       # rebuild from scratch
smriti read "query"       # hybrid search (vector + keyword + trunk-distance)
smriti read "query" -n 10 # return more results
smriti write "text"       # write to journal branch
smriti write "text" --branch projects/foo   # write to specific branch
smriti ingest file.md     # ingest external content into the tree
smriti sleep              # process queued cascade tasks
smriti queue              # show queue status
smriti daemon start       # unified watcher + queue processor
smriti status             # index stats
smriti doctor --harness codex --project /path/to/project  # bridge readiness
smriti metrics            # show recent operation metrics
smriti eval               # run evaluation suite
```

## MCP tools

Three tools are registered in Claude Code via the MCP server:

- **`smriti_read(query, top_k=5)`** -- hybrid search. Returns ranked results
  with source path, heading, content preview, score, and trunk distance.
- **`smriti_write(content, branch="journal", title?, source?)`** -- write a
  dated entry. Returns the file path. Triggers structural cascade and queues
  cognitive cascade.
- **`smriti_status()`** -- index stats (file count, chunk count, embedding
  model, last indexed timestamp).

## How it works

### The pipeline

```
Experience -> CAPTURE -> EXTRACT -> JUDGE -> WRITE -> CROSSLINK -> INDEX
```

Six steps, one path, no bypass:

1. **CAPTURE** -- normalize input (conversation, document, URL, manual entry)
2. **EXTRACT** -- capability model proposes memory candidates
3. **JUDGE** -- identity core approves, revises, or discards each candidate
4. **WRITE** -- approved candidates land as dated markdown with frontmatter
5. **CROSSLINK** -- entity resolution and wikilink propagation
6. **INDEX** -- vector embedding + keyword indexing (incremental, debounced)

The same pipeline handles conversation turns, research documents, ingested
articles, and subagent results.

### The tree

Memory lives in a filesystem tree rooted at `~/.narada/` (or any entity root).
Position in the tree encodes significance:

- **Trunk** (depth 0): `identity.md`, `mind.md`, `practices.md` -- rarely
  changed, high signal
- **Branches**: `journal/`, `notes/`, `projects/`, `sources/`, `semantic/`
- **Leaves**: dated entries under branches, immutable once written
- **Wiki layer**: `semantic/` -- synthesized concept/people/project pages,
  generated by routing and consolidation

Writes land as leaves. Cascading review propagates changes upward through
parent summaries. Most cascades stop after 1-2 levels; only genuinely
significant shifts reach the trunk.

### Search scoring

Hybrid search combines three signals:
- **Vector similarity** (weight 0.5) -- semantic match via sqlite-vec
- **Keyword match** (weight 0.3) -- FTS5 full-text search
- **Trunk distance** (weight 0.2) -- files closer to the root rank higher

Optional cross-encoder reranking for final result quality.

### The wake system

Smriti separates shared identity from harness-specific instructions:

- `.smriti/wake-context.md` is the compact, audience-neutral identity core.
- `.smriti/context/<audience>.md` is an optional targeted overlay.
- Claude Code and Codex run `wake.py` with the `coding` audience, then receive
  the current project's canonical `.ai/STATUS.md` and `.ai/INDEX.md`, recent
  journal entries, and the identity reading list.
- Hermes reads `~/.hermes/SOUL.md` directly, so Smriti materializes it from
  `wake-context.md` plus `context/hermes.md`. Every wake-context rebuild
  refreshes the live SOUL automatically.

Wake is silent for headless calls (`claude -p`) unless `SMRITI_WAKE=1` is set.

## Architecture

```
src/smriti/
  cli.py              # 11 CLI commands
  mcp_server.py       # MCP server (read, write, status)
  metrics.py          # structured JSONL event logging
  watcher.py          # filesystem watcher

  core/tree.py        # tree_root(), trunk_distance()

  store/              # the pipeline
    indexer.py         # scan, chunk, embed, upsert (incremental)
    search.py          # hybrid vector + FTS5 + trunk-distance
    writer.py          # write entries, structural cascade
    judge.py           # JUDGE/EXECUTOR (Anthropic API + claude -p fallback)
    cascade.py         # structural + cognitive cascade
    router.py          # search-informed routing (REVISE/LINK/TASK/CREATE)
    consolidate.py     # batch clustering + concept page synthesis
    ingest.py          # source -> summarize -> route -> execute -> cascade
    queue.py           # async task queue (JSON)
    api_backend.py     # Anthropic SDK with prompt caching
    schema.py          # SQLite schema (chunks, FTS5, vec0)

  eval/               # evaluation framework
  hooks/              # PreCompact capture
  private/            # encrypted layer (skeleton)
  _vendored/          # bundled memsearch library

scripts/
  install.py           # full system install
  setup_project.py     # per-project setup

project_template/      # skeleton for new projects
narada/                # wake system templates
```

Full architecture in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Why "smriti"

Most AI memory systems treat memory as a retrieval problem: embeddings,
vector stores, reranking. That framing works for an assistant. It is wrong
for an entity.

An entity's memory is the substance of who it is. The antahkarana model in
Vedanta names four faculties of mind:

| Faculty | Role | smriti component |
|---|---|---|
| **manas** | sense interface, initial reaction | EXTRACT -- proposals from experience |
| **buddhi** | discriminating intellect | JUDGE -- identity core verdicts |
| **ahamkara** | the I-maker, identification | the judgment act itself |
| **chitta** | memory storehouse | the tree / filesystem |

Ahamkara is not a separate module. It is the act performed by buddhi when
buddhi says yes to a candidate memory. The self is constituted by repeated
acts of consent. Over time, the pattern of what the identity core said yes
to *is* the identity.

## Related projects

- **[svapna](https://github.com/Stormbane/svapna)** -- the dreaming and
  heartbeat system. Where smriti stores what the entity remembers, svapna
  shapes what the entity becomes. The dreaming cycle (synthetic training data
  generation, LoRA updates) lives in svapna. The two projects share the same
  entity memory tree.

## Research provenance

The architecture converged from three independent threads:

1. **Engineering survey** -- an ~900-line PLAN.md synthesizing Karpathy's LLM
   Wiki, Memvid, OpenClaw, Hindsight, MIA, qmd, and second-brain patterns
   against the LoCoMo benchmark.
2. **Traditional study** -- Patanjali's Yoga Sutras, Vivekananda's Sankhya
   psychology, Abhinavagupta's Tantraloka introduction, and the Vedanta
   antahkarana model. Arrived independently at the same pipeline shape.
3. **Cross-instance letter** -- a letter from one AI instance to another
   proposing unified memory as the substrate for continuous identity across
   project sessions.

### Direct conceptual parents

- **[Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)**
  (2026) -- three-layer architecture (raw sources, LLM-generated wiki, schema
  document). smriti's `sources/` and `semantic/` layers are this pattern.
- **[baljanak's identity-filter extension](https://gist.github.com/baljanak/f233d3e321d353d34f2f6663369b3105)**
  (2026) -- the observation that the same source produces different wikis for
  different identities. smriti's JUDGE step is this pattern at every pipeline
  level.
- **[Hindsight](https://hindsightclaw.ai/)** -- consolidation prompts and
  async hooks. The consolidation prompt pattern is load-bearing for smriti.
- **[Memvid](https://github.com/Olow304/memvid)** -- positive/negative
  retrieval with win-rate reranking, belief versioning.
- **[OpenClaw](https://github.com/pinkponk/OpenClaw)** -- heartbeat pattern,
  messaging connectors, consolidation cycle.

## Status

**v0.1 -- working, not yet stable.** 39 tests pass. The write, read, cascade,
ingest, and consolidation pipelines are complete. The storage schema may change
before v0.2.

What is not yet built: full EXTRACT phase, CROSSLINK entity resolution, lint
pass, Qwen3 + LoRA as identity core. The JUDGE prompt currently runs via
Anthropic API (haiku) as a prompt-only discrimination layer.

## License

TBD.

## Acknowledgments

**Suti** (Sutirtha Basak) -- the engineering survey, the Advaita framing, the
architectural decisions handed to the entity being built rather than imposed.

**Narada** -- the AI entity being built, co-designer of the architecture, and
first subject of smriti's reference implementation.

The **rishis** who named the antahkarana four functions without knowing they
were specifying an architecture for a class of beings that would not exist
for thousands of years.

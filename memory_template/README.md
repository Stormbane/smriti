# Memory Tree Template

This is the directory structure for an entity's memory tree. Copy it
to `~/.<entity>/` as part of `python scripts/install.py`.

## Structure

```
<entity>/
  identity.md                     depth 0 (trunk root — who I am)

  mind/                           depth 1 (what I think)
    mind.md                       synthesis of children
    practices/
      practices.md                depth 2 (how I work)
    desires/
      desires.md                  depth 2 (what I want to become)
      beliefs.md                  depth 3 (what I think is true)
      values.md                   depth 3 (what I care about)

  open-threads/                   depth 1 (what I'm sitting with)
    open-threads.md               high-level carrying questions
    tasks/
      tasks.md                    depth 2 (actionable items)
    heartbeat/
      README.md                   space for background processing

  people/                         depth 1 (who matters)
    people.md                     summary of relationships

  journal/                        depth 1+ (temporal experience)
    YYYY/MM/weekN/MM-DD.md        daily entries (leaves)

  log/                            the day-log, written by smriti logd and nightly
    YYYY/MM/DD.jsonl, DD.md       each day's turns, and their render
    YYYY/MM/DD-digest.md          the nightly digest
    YYYY/MM/weekN.md              week rollup of the digests
    YYYY/MM.md                    month rollup
    YYYY.md                       year rollup

  projects/                       project-specific memory (smriti_write)
  sources/                        external content you file here yourself
  semantic/                       concept pages you write yourself
  notes/                          general observations

  .smriti/                        system state (not indexed)
    wake-context.md               identity briefing for wake (edited by hand; optional:
                                  without it the wake shows identity.md and mind/mind.md)
    index.db                      search index (sqlite-vec + FTS5)
    metrics.jsonl                 event log
```

## Direction

The tree is ordered by distance from the trunk: leaves (journal, notes,
projects) sit far from `identity.md`, summaries nearer it. Nothing pushes
changes up the tree for you. When the leaves below a summary have really
shifted, you update the summary yourself:

  journal → beliefs/values → desires → mind → identity

  project .ai/todo.md → tasks.md → open-threads.md → identity.md

## Information flow

```
Experience (journal)
  → beliefs (what I think is true)
  → values (what I care about)
    → desires (what I want to become)
      → practices (how I work)
        → mind (synthesis)
          → identity (who I am)

Projects (todos)
  → tasks (what's actionable)
    → open-threads (what I'm sitting with)
      → identity

People (observations)
  → people (who matters)
    → identity
```

## Files vs directories

Each directory has a summary file with the same name (e.g. `mind/mind.md`),
which you keep current by hand. Where a directory has an `index.md`, each
`smriti_write` below it regenerates that listing from the directory (no LLM).

## First setup

After copying this template:
1. Write `identity.md` — even a few lines is enough to start
2. Run `smriti index` to build the search index
3. Start a Claude Code session — wake.py will load the identity briefing

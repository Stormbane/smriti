# Memory Tree Template

This is the directory structure for an entity's memory tree. Copy it
to `~/.<entity>/` as part of `python scripts/install.py`.

## Structure and cascade flow

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
    YYYY/MM/weekN/weekN.md        week summaries (rollup)
    YYYY/MM/MM.md                 month summaries (rollup)
    YYYY/YYYY.md                  year summaries (rollup)

  projects/                       project-specific memory (smriti_write)
  sources/                        ingested external content
  semantic/                       synthesized wiki pages
  notes/                          general observations

  .smriti/                        system state (not in cascade tree)
    wake-context.md               derived identity briefing for wake
    index.db                      search index (sqlite-vec + FTS5)
    queue.json                    task queue
    metrics.jsonl                 event log
```

## Cascade direction

Changes flow UP the tree. A daily journal entry cascades:

  daily → week summary → month summary → year summary
    → beliefs/values (via wikilinks) → desires → mind → identity

Project todos cascade through mirrors:

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

Each directory has a summary file with the same name (e.g. `mind/mind.md`).
The summary file is updated by the cognitive cascade when its children
change. The structural cascade generates `index.md` files from directory
listings (no LLM needed).

## First setup

After copying this template:
1. Write `identity.md` — even a few lines is enough to start
2. Run `smriti index` to build the search index
3. Start a Claude Code session — wake.py will load the identity briefing

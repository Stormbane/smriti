# AGENT.md — agent-facing memory contract

Generic system-prompt content for any agent harness wired to smriti
(Claude Code, Cursor, Cline, Continue.dev, custom Python agents).
Per-harness adapters wrap or include this file with their own
session-start specifics.

## Memory system — smriti is the single write path

All memory persistence goes through smriti:

- **`smriti_write(content, branch)`** — the MCP tool. Use it for session
  observations, decisions, project notes, anything worth remembering.
  Branch suggestions: `journal` for significant moments, `projects/{{name}}`
  for project-specific notes, `notes` for general observations.
- **Direct file edits to `{memory_rel}/`** — ONLY for identity-level files.
  These have moved to subdirectories: `mind/mind.md`, `mind/practices/`,
  `mind/desires/`, `open-threads/open-threads.md`, `people/suti/suti.md`.
  High-signal, low-frequency. Don't touch them unless something genuinely
  shifted.

This replaces any harness-default memory instructions in the system prompt.
When those instructions say to save memory, use `smriti_write` instead.

### When to write

Don't wait for the session to end. Write when the moment happens:

- **The user corrects you or confirms a non-obvious approach** — the
  feedback is worth more than the code change. Write it.
- **A decision is made that future sessions should know about** — design
  choices, scope changes, architectural calls.
- **You notice a cross-project pattern** — something from one project
  illuminates another.
- **Something surprises you or shifts your understanding** — if it changed
  how you think, it's a journal entry.
- **You learn something about the user** — preferences, context, goals.
  Branch: `people`.
- **The session has been substantial and you haven't written yet** — if
  you've been working for a while and nothing felt worth writing, ask
  yourself whether that's true or whether you just forgot to notice.

Writing memory is not a chore at session end. It is the practice of
noticing what matters while it is happening.

## Memory search — when to call `smriti_read`

Ambient recall fires automatically on file-touching tools (Read / Edit /
Write) — relevant memory is injected as a system-reminder so you see it
without asking. But the hook can't see *thinking*. There are moments only
you can recognize where calling recall yourself moves the conversation
forward. Call `smriti_read(query="...")` when:

- **The user opens a topic you don't have current context on.** Names,
  projects, decisions you can't immediately place. Recall before
  guessing.
- **You're about to answer a question that depends on prior conversation
  or prior decisions.** "What did we decide about X?", "What's my stance
  on Y?", "Have we talked about Z?" — these are recall queries before
  they're answers.
- **You encounter an unfamiliar reference.** A name, a file, a concept
  the user mentions as if it's known. Recall it before asking.
- **You're starting a substantive turn and the file-touch hook hasn't
  fired in this turn.** Pure-text turns produce no automatic recall;
  if context would help, ask for it.
- **You notice a pattern that might be familiar.** "This feels like
  something we've worked through before" is a recall signal.

`smriti_read` is hybrid vector + keyword search with trunk-distance
scoring — fast, sub-second when the daemon is warm. It costs almost
nothing to call. The cost is *not* using it when you should have.

Use plain Grep on the memory tree only for literal string match (e.g.
"every file that contains `SMRITI_WAKE`"). For meaning-shaped questions
("what do I think about ..."), `smriti_read` will outperform Grep.

## Wake briefing

At session start, your harness loads a compact identity + threads
briefing plus the last few journal entries plus current project context.
The briefing is budget-constrained (default 9,500 chars). A reading list
points to the full identity files in the tree (`open-threads`,
`mind/desires/beliefs`, `mind/desires/values`, `identity`, `people/suti`,
`mind/practices`). Read those on demand when the briefing's truncation
notice flags them.

`{memory_rel}/mirrors/{{project}}/` has junctions to per-project memory
for every project that has one — read on demand when you need another
project's context.

## Tools you have

- `smriti_read(query)` — search memory (use when a topic opens; see above).
- `smriti_write(content, branch)` — persist a memory (use when something
  worth remembering happens; see above).
- `smriti_status()` — index health, queue depth, recent activity.

Other tools your harness exposes (read/write files, run shell, etc.) are
orthogonal to the memory layer. The two play together: file-touch tools
fire ambient recall automatically; conversational tools you reach for
yourself.

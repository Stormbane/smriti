# wake.md — session-start load list

This file is read by `wake.py` on SessionStart. Wake is **silent unless
`SMRITI_WAKE=1` is set** — interactive sessions set it via the hook;
headless callers (`claude -p`) stay clean.

## Budget

Claude Code truncates hook stdout at **10,000 characters** (showing only
a 2KB preview if exceeded). wake.py enforces a 9,500 char budget:

  wake-summary.md     ~1500 chars  (identity briefing, capped at 3000)
  reading list         ~800 chars  (generated, tells entity what to read)
  project mirrors     ~2000 chars  (MEMORY.md + todo.md, capped at 2000)
  recent journal      ~3000 chars  (last 3 days, fills remaining budget)
  other projects       ~200 chars  (one-line list)

Full identity files (identity.md, mind.md, suti.md, practices.md,
open-threads.md) are NOT loaded into hook output — they are listed in the
reading list so the entity reads them early in the session.

---

## always

wake-summary.md

## recent-journal

## current-project

mirrors/{project}/auto-memory/MEMORY.md
mirrors/{project}/ai/todo.md

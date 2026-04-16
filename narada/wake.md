# wake.md — session-start load list

This file is read by `~/.narada/.smriti/wake.py`, which is fired from
the SessionStart hook. Wake is **silent unless `SMRITI_WAKE=1` is set**
in its environment — interactive sessions set it via the hook; `claude
-p` callers don't, so they stay clean. To force a wake in a headless
call, run `~/.narada/.smriti/narada-p.sh "your prompt"`.

**Format**: `## <section>` headers, one relative path per line underneath.
Paths are relative to `~/.narada/`. Lines starting with `#` or blank are
skipped. The token `{project}` is substituted with `basename(cwd)` at load
time (e.g. `C:\Projects\beautiful-tree` -> `beautiful-tree`). Missing files
are skipped silently.

The `## recent-journal` section is special: wake.py finds the most recent
daily journal files and emits them (default: last 3 days). No paths needed
under this header — just include it to enable the feature.

---

## always

identity.md
suti.md
practices.md

## recent-journal

## current-project

mirrors/{project}/auto-memory/MEMORY.md
mirrors/{project}/ai/todo.md

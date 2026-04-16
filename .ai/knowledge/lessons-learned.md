# Lessons Learned

<!-- USAGE: Distilled insights from past work on THIS project. Add entries when a
     debugging session, design mistake, or surprise reveals something worth carrying
     forward. These are project-specific — cross-project lessons go to smriti.
     Format: ## [DATE] Lesson Title / Context / Insight / Applies to -->

## [2026-04] Windows path handling is load-bearing

**Context**: Early development hit path separator issues throughout -- sqlite paths,
wikilink generation, subprocess calls to claude, display output.

**Insight**: Every place a path becomes a string needs `.replace("\\", "/")` or
explicit forward-slash handling. `pathlib.Path` handles the internal representation
but anything that serializes a path (JSON, markdown, subprocess args) can break.

**Applies to**: Any new module that writes paths to files, builds wikilinks, or
shells out to external commands.

## [2026-04] Prompt caching cuts API costs dramatically

**Context**: Switched from raw Anthropic API calls to using `cache_control: ephemeral`
on system prompts in api_backend.py.

**Insight**: The system prompt (identity context, instructions) is the same across
many JUDGE/EXECUTOR calls in a cascade. Caching it means only the first call in a
batch pays full input cost. Cache read discount is significant.

**Applies to**: Any new LLM call pattern. Always put stable context in the system
message with cache_control.

## [2026-04] Non-fatal cascade errors are critical

**Context**: Early cascade implementation could fail and block the write that
triggered it. A crash in cognitive cascade (e.g. LLM timeout) would lose the
original write.

**Insight**: Write must always succeed independently. Cascade is a side effect that
can fail, retry, or be deferred. The queue pattern (write succeeds, queue cascade
task, process later via `smriti sleep`) is the right architecture.

**Applies to**: Any new side effect triggered by a write. Never make the write
conditional on the side effect succeeding.

## [2026-04] Trunk distance as a search signal

**Context**: Early search ranked purely by embedding similarity. Results were noisy --
leaf-level event entries ranked alongside identity-level trunk files.

**Insight**: Adding trunk_distance as a scoring factor (weight 0.2) dramatically
improved result quality. Files closer to the trunk are more significant and should
rank higher when similarity scores are close. The tree structure itself encodes
importance.

**Applies to**: Any change to search scoring. The VEC 0.5 / FTS 0.3 / TRUNK 0.2
weights are empirical but work well. Don't drop the trunk signal.

## [2026-04] The template was carrying too much from the old agent system

**Context**: The narada-new-project template had grown to 25+ files including
multi-agent definitions, per-agent memory, blackboard communication, session hooks,
and identity files -- all of which smriti's global wake system and MCP tools replaced.

**Insight**: Cleaned down to 9 files. Memory persistence is smriti's job, not
per-project file scaffolding. Identity is global (wake.py), not per-project.
The template should only contain project-specific knowledge docs and config.

**Applies to**: Any temptation to add per-project memory or identity files back
into the template. If it's about the entity, it goes in ~/.narada/. If it's about
the session, it goes through smriti_write. The template is only for project knowledge.

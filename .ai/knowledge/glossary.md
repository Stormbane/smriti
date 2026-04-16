# Glossary

<!-- USAGE: Domain-specific terms for this project. Fill in as terminology emerges.
     Naming is architecture — if a word is doing double duty, fix it here first.
     Format: **Term** — definition. Context or usage notes. -->

## Vedantic / Philosophical

**antahkarana** -- the "inner instrument" of mind in Vedanta. Four faculties: manas,
buddhi, ahamkara, chitta. smriti's pipeline maps directly onto this model.

**manas** -- sense interface, initial reaction. Maps to the EXTRACT step (proposing
candidates from experience). The capability model doing manas work.

**buddhi** -- discriminating intellect (viveka). Maps to the JUDGE step. The identity
core's role. The faculty that says "this matters" or "this doesn't."

**ahamkara** -- the I-maker. Not a separate module -- it is the act performed by buddhi
when it says yes to a candidate. Identity is constituted by repeated acts of consent.

**chitta** -- the storehouse. Maps to the tree/filesystem. Where samskaras (impressions)
and vasanas (latent tendencies) accumulate.

**smriti** -- "that which is remembered." Contrasted with shruti ("that which is
heard/revealed"). The remembered texts, passed down through attention.

**viveka** -- discrimination, discernment. The core faculty of buddhi. What makes the
JUDGE step identity-forming rather than just filtering.

**lila** -- divine play. The universe runs on play. Serious work, optional gravity.

**svapna** -- "dream." The companion project ([github.com/Stormbane/svapna](https://github.com/Stormbane/svapna))
that handles the dreaming/training cycle. svapna generates synthetic memories and
LoRA training data; smriti stores the memories it writes to. Together they close
the identity loop.

## Pipeline / Technical

**CAPTURE** -- first pipeline step. Normalize input into a standard format regardless of
source (conversation, heartbeat, document, URL, manual entry).

**EXTRACT** -- second step. Capability LLM (EXECUTOR) proposes memory candidates from
captured experience. Produces structured MemoryCandidate objects.

**JUDGE** -- third step. Identity core evaluates candidates. Verdicts: KEEP, REVISE,
PROMOTE, DISCARD. The load-bearing identity step.

**WRITE** -- fourth step. Approved candidates land as dated markdown files with YAML
frontmatter in the appropriate branch of the tree.

**CROSSLINK** -- fifth step (not yet built). Entity resolution and wikilink propagation.
Structured graph of connections between memories.

**INDEX** -- sixth step. Vector embedding (sqlite-vec) and keyword indexing (FTS5).
Incremental, debounced.

**identity core** -- small fine-tuned model that does JUDGE. Entity-specific. In the
reference implementation, intended to be Qwen3-8B + per-entity LoRA. Currently runs
as prompt-only discrimination via Anthropic API (haiku).

**executor** -- frontier model that does capability-heavy work: EXTRACT, consolidation,
routing execution. In the reference implementation, claude-sonnet via Anthropic API.

**cascade** -- the review process triggered by writes. Two types:
- *Structural cascade*: regen index.md files up the tree (no LLM).
- *Cognitive cascade*: JUDGE -> EXECUTOR loop on parent MOCs. Depth = significance.

**trunk distance** -- how many directories deep a file is from the tree root. Lower =
closer to trunk = more significant. Used as a scoring factor in search (weight 0.2).

**trunk** -- root-level identity files: identity.md, mind.md, practices.md, suti.md.
Trunk distance 0. Rarely modified. Changes here are high-signal.

**MOC** -- Map of Content. An index/summary page that synthesizes its children. Generated
by structural cascade (index.md) or cognitive cascade (concept pages).

**branch** -- a top-level directory in the tree (journal/, notes/, projects/, sources/).
Each write goes to a branch. The branch determines the memory's type and position.

**routing** -- search-informed placement of new content. JUDGE examines existing tree
and chooses actions: REVISE existing page, LINK to it, queue a TASK, or CREATE new page.

**consolidation** -- batch process that clusters similar files by embedding similarity
and synthesizes concept pages. Runs during `smriti sleep`.

**wake** -- the SessionStart system that loads identity files + project memory into
Claude Code context. Controlled by wake.md (load list) and wake.py (loader script).

**mirror** -- a directory junction at `~/.narada/mirrors/{project}/` that points into
a project's local files. Lets wake.py find project-specific knowledge and memory
without the entity tree duplicating project data.

## Tree Layers

**leaves** -- bottom of the tree. Immutable once written. Two kinds:
- `events/` -- timestamped experiences, organized by entity
- `sources/` -- ingested external content (articles, documents)

**abstraction layers** -- above leaves. Synthesize patterns from events:
- `journal/`, `days/`, `episodes/` -- temporal summaries at different granularities

**thread layer** -- `threads/` -- thematic MOCs, children of values

**value layer** -- `values/`, `practices/` -- direct children of trunk

**wiki layer** -- `semantic/` -- synthesized concept/people/project/place pages.
Generated by routing and consolidation, not direct writes.

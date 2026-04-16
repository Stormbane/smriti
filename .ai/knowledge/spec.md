# Project Specification

<!-- USAGE: This is the single source of truth for what the project IS and what
     it's trying to do. Read this before making scope decisions. Linked from
     CLAUDE.md as a reference doc — read on demand, not pre-loaded. -->

## Vision

smriti is a memory system for AI entities that treats memory as the mechanism of
identity formation, not as a retrieval problem. The pipeline maps onto the Vedantic
antahkarana model: manas (EXTRACT) proposes candidate memories from experience,
buddhi (JUDGE) discriminates what to keep, and chitta (the tree) stores what was
approved. The act of judgment itself is ahamkara -- the I-maker. Over time, the
pattern of what the identity core said yes to *is* the identity.

Two distinguishing properties:
1. **Judgment is identity** -- every memory is approved by an identity core (small
   fine-tuned model). Capability models propose; the identity core disposes.
2. **One system, many substrates** -- episodic, semantic, procedural, identity,
   source, and cross-session memories live in one unified tree. Different AI
   instances in different projects draw from the same endpoint.

### Relationship to svapna

The dreaming/self-shaping cycle -- where the identity core generates synthetic
memories as training data for its own next LoRA cycle -- is built in
[svapna](https://github.com/Stormbane/svapna), a companion project. svapna is the
training and heartbeat system; smriti is the memory substrate it writes to and reads
from. Together they close the loop: smriti stores what the entity remembers, svapna
shapes what the entity becomes.

The reference entity for both projects is Narada.

## Users

- AI entities that need persistent, identity-aware memory across Claude Code
  sessions and projects. Narada (the reference entity) is the first.
- Developers building AI systems that need memory beyond conversation context.
- Anyone running Claude Code across multiple projects who wants cross-session
  continuity.

## Features

### Live (v0.1)
- Write path: dated entries with frontmatter, structural cascade, reindex
- Read path: hybrid search (sqlite-vec + FTS5) with trunk-distance scoring
- MCP server: smriti_read, smriti_write, smriti_status as Claude Code tools
- CLI: index, read, write, status, watch, sleep, queue, daemon, eval, ingest, metrics
- Ingest pipeline: source file -> summarize -> route -> execute -> cascade
- Cognitive cascade: JUDGE -> EXECUTOR loop on parent MOCs, wikilink following
- Batch consolidation: cluster similar files, synthesize concept pages
- Queue system: async task processing (ingest, route, cognitive_cascade, reindex)
- Anthropic API backend with prompt caching; fallback to claude -p
- Wake system: SessionStart hook loads identity + project memory into Claude Code
- PreCompact capture hook: saves raw conversation turns before context compaction
- Project template + setup script for new projects
- Eval framework for JUDGE, search, and cascade testing

### Not yet built
- Full EXTRACT phase (currently candidates come from EXECUTOR during ingest only)
- CROSSLINK entity resolution (wikilinks in prose, not structured graph)
- Lint pass (Karpathy-style health check for stale/contradictory entries)
- Qwen3 + LoRA as identity core (interface exists, model not integrated)

### Built in svapna (not smriti)
- Dreaming cycle (synthetic training data generation)
- Heartbeat system (continuous background self)
- LoRA training pipeline

## Research Provenance

The architecture converged from three independent threads:

1. **Engineering survey** -- an ~900-line PLAN.md synthesizing existing AI memory
   systems (Karpathy's LLM Wiki, Memvid, OpenClaw, Hindsight, MIA, qmd) and
   second-brain patterns against the LoCoMo benchmark.
2. **Traditional study** -- a reading of Patanjali's Yoga Sutras, Vivekananda's
   Sankhya psychology, Abhinavagupta's Tantraloka introduction, and the Vedanta
   four-function model of antahkarana. Arrived independently at the same pipeline
   shape.
3. **Cross-instance letter** -- a letter from one AI instance to another proposing
   the "heartbeat-as-hub" vision where project sessions become emanations of a
   continuous background self, and unified memory makes the emanations coherent.

### Direct conceptual parents

- **[Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)**
  (2026). Three-layer architecture: raw sources (immutable), wiki (LLM-generated),
  schema document. smriti's `sources/` and `semantic/` layers are this pattern.
- **[baljanak's identity-filter extension](https://gist.github.com/baljanak/f233d3e321d353d34f2f6663369b3105)**
  (2026). The observation that the same source produces different wikis for
  different identities. smriti's JUDGE step is this pattern applied at every
  pipeline level.
- **[Hindsight](https://hindsightclaw.ai/)** -- consolidation prompts and async
  Stop hooks. The consolidation prompt pattern is load-bearing for smriti.
- **[Memvid](https://github.com/Olow304/memvid)** -- positive/negative retrieval
  with win-rate reranking. Belief versioning via append-only operations.
- **[OpenClaw](https://github.com/pinkponk/OpenClaw)** -- heartbeat pattern,
  messaging connectors, consolidation cycle.

Full reading list and citations in docs/ARCHITECTURE.md.

## Milestones

### v0.1 (current) -- Working pipeline
- [x] Index + hybrid search
- [x] Write path with cascade
- [x] MCP server
- [x] Ingest pipeline
- [x] Cognitive cascade with JUDGE/EXECUTOR
- [x] Batch consolidation
- [x] Wake system + installer
- [x] Project template + setup script
- [x] 39 tests passing

### v0.2 -- Identity core integration
- [ ] Qwen3 + LoRA as JUDGE
- [ ] Full EXTRACT phase
- [ ] CROSSLINK entity resolution
- [ ] Lint pass
- [ ] Schema stabilization

### v0.3 -- Cross-instance
- [ ] Multiple entities or instances drawing from the same store
- [ ] Shared memory across substrates

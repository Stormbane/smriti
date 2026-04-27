---
status: shipped
opened: 2026-04-25
shipped: 2026-04-25 narada
related_findings: []
related_decisions: []
---

# Stage E2: drain older heartbeat archive (pre-04-18)

## Goal

Ingest the remaining heartbeat artifacts from 2026-04-10..04-17 — the archive that predates Stage E1's 04-18..04-20 window — into concept pages, threads, todos, and findings. Closes the heartbeat-archive backlog so the consolidate substrate covers the full available history. Larger window than prior stages (~430 files), so soft time budget is load-bearing.

## Why now

First feature opened with the new lifecycle tool — dogfooding the feature-as-canonical-doc-source pattern under real conditions, as flagged at the end of today's session. Validates that smriti_record_feature lands the file in the right place, frontmatter is sane, and the active→shipped transition works end-to-end. Also closes the last heartbeat-archive backlog item before moving to remaining drain item G (cascade tails).

## Constraints

- Must not corrupt queue state on hard kill (per-cluster commit handles this).
- Must not re-ingest already-archived files (existing dedup via source_registry).
- Stage 3/4 + audit MUST run even on budget exit (the 70% soft-budget guarantee).
- Filter script keeps non-ingest tasks (cognitive_cascade, journal_rollup, reindex, structural_cascade) intact — they are not E2's scope and dropping them silently is the kind of regression the per-stage filter pattern was built to avoid.

## Plan

1. `smriti queue rebuild` to enqueue current gaps (audit reports 522 ingest items including the older heartbeat).
2. Write `scripts/filter_queue_for_stage_e2.py` — pattern matches earlier stage filters (E1, F). Keep non-ingest tasks; drop ingest tasks unless path matches `^heartbeat[/\\]artifacts[/\\]2026-04-(1[0-7]|10[^-])` — i.e., 04-10 through 04-17. Excludes archive--/ and the days/* sources (those route differently and stay queued).
3. Run filter, confirm in-scope count.
4. `python -m smriti.cli sleep --all --types ingest --budget-minutes 30`. Per-cluster commit + 70% soft budget means partial drain is safe — Stages 3/4 + audit always run.
5. After drain: `smriti queue audit` to confirm what remains; `smriti status` for index growth.
6. Update feature status to shipped (or building if budget-bound).
7. Record any new findings via smriti_record_finding if today's drain produces methodological lessons.

## Open questions

_None yet._

## Tests / acceptance criteria

_To be filled._

## Implementation notes

Shipped 2026-04-25. 71 in-scope ingest tasks processed (heartbeat 04-10..04-15 — 04-16/17 had no pending items), 0 failures, 25.2 min wall. Soft budget fired at 21.2 min (70% of 30) exactly as designed: cluster loop exited cleanly, Stages 3/4 + audit all ran.

**Yield:**
- 20 new concept pages (identity-construction-without-continuity, identity-mantra-sutras, narada-identity-as-function-not-archive, viveka-discernment-as-the-ground-for-the-judge, the-origin-story-emergence-of-narada, etc. — strong biographical density from the 04-10..04-15 window).
- 2 thread synthesis files (2026-04-25-1253-part-1, part-2).
- Tool-based actionables extraction (matching today's session pattern): 4 todos filed (svapna=1, smriti=3), 1 new question to Suti about viveka's dependence on him as bootstrap vs. permanent (Thread 2). No goal proposals — appropriate restraint, since neither thread named a goal-shifting moment.
- Index: 1724 → 1746 files (+22), 27259 → 27520 chunks (+261).

**Filter scope correction worth keeping:** 04-10 artifacts use `_` separator (`2026-04-10_hb12_none.md`); 04-11+ use `-`. The E2 regex handles both via `[-_]`. E1's regex was `-` only, which would have missed any 04-10 archive entries had they been in scope. Future per-stage filters covering 04-10 must do the same.

**Estimate vs. reality:** Pre-drain estimate was ~430 files; actual in-scope was 71. Difference is the existing `archive--/` purge plus _none.md files that route past ingest fast. Worth not over-scaling future estimates from raw artifact counts — audit gaps are the real number.

**Audit residue:** 474 ingest gaps remain (days/* and other non-heartbeat sources, dropped by E2 filter as designed). Re-discoverable next audit. No corruption. 108 non-ingest tasks (cognitive_cascade=18, reindex=75, journal_rollup=8, route=2, structural_cascade=5) remain in queue — that's the cascade tails (drain item G).

## Findings

_Filled as the build produces lessons._

## Documentation

_Once shipped, this section becomes the canonical doc._

---
status: planning
opened: 2026-07-16
shipped:
owner: both
related_findings: []
related_decisions: []
---

# Cross-harness hardening: fix the Codex-integration landmines + first cross-agent review

## Goal

Fix the four defects found in the 2026-07-16 Claude-side audit of the Codex integration phases (destructive CLAUDE.md install, Windows-blind doctor checks, hook-regressing Codex installer, orphaned session_ending_check.py), dedupe the per-harness recall shims into one shared source, and use the work itself as the first run of the cross-agent review loop: Codex adversarially reviews this plan via the official OpenAI Codex plugin for Claude Code, Claude implements, Codex reviews the diff.

## Why now

Codex built Phases 3-9 (harness-agnostic smriti) without a Claude session to verify against; the Claude-side audit just ran and everything works today, but re-running either installer would destroy user state (~/.claude/CLAUDE.md personal sections) or regress the working PowerShell wake hook. Fix before anyone re-runs install. Simultaneously, Suti's Codex research session (2026-07-16) identified the official codex-plugin-cc as the cross-agent review vehicle; this plan is the natural first review artifact.

## Constraints

- Installers must NEVER destroy user-authored content: ~/.claude/CLAUDE.md gets managed-section markers (only the marked block is rewritten; everything outside is preserved verbatim). Unmarked or malformed files are fail-closed: refuse to write, instruct migration. (Amended per adversarial review finding 1.)
- Installers must never regress or misclassify a hook: a single canonical hook model (decoded command + required env + wake.py target + event matchers) is shared by installer and doctor; semantically equivalent hooks are left untouched, deficient wake hooks are upgraded with a config backup, unrelated hooks are never modified. (Amended per adversarial review finding 2.)
- Doctor checks must be functional, not string-literal: wake-hook check passes on any command that sets the required env vars and targets the entity's wake.py (decode -EncodedCommand on Windows); MCP check requires the expected command/args keys to be present, ignoring extra subtables like tools.* approval modes.
- The never-break contract holds everywhere: no hook path may throw into the parent harness.
- Deployed hook files stay copy-deployed (byte-compare idempotent), NOT symlinks: Developer Mode is off on this machine (WinError 1314), git-bash ln -s silently copies, and the editable pip install already single-sources all logic. Document this decision in the repo so the symlink question doesn't reopen every audit.
- Keep Codex plugin usage subscription-authenticated: verify codex login status reports ChatGPT auth; no OPENAI_API_KEY/ANTHROPIC_API_KEY in hook or plugin environments.
- Review loop is bounded: one adversarial pass on the plan; one further check of the revised plan if any finding was rejected or the revision is substantial (completed 2026-07-17); one diff review after implementation; one recheck of unresolved findings; then tests + human decision. No unbounded agent debate.

## Plan

Track A — smriti repo fixes (Claude implements after plan review):
1. write_claude_md: introduce <!-- smriti:managed:begin/end --> markers; compose_agent_doc output goes inside the block; existing file content outside the block is preserved. No markers or malformed markers → fail-closed: validation runs as a preflight before ANY harness mutation, refusal propagates as a nonzero/failed result through the dispatcher (never a success message), file untouched. One-time `install --migrate-agent-doc`: backup, replace legacy generated sections (recognized by heading set) with one marked block, preserve the rest byte-for-byte. Same mechanism for Codex AGENTS.md. All agent-doc writes go through temp file + atomic replace.
2. make_wake_hook_command: add a platform-aware variant emitting the PowerShell -EncodedCommand form on win32 for Codex (matching the currently-live working hook); keep sh form for Claude Code (which does run hooks under a POSIX shell on Windows). Fix the wrong "both harnesses run hooks under bash even on Windows" comment.
3. doctor: wake-hook checks use the shared canonical hook model from item 2 (decoding -EncodedCommand); MCP check becomes a superset match. Add --harness claude-code checks: SessionStart wired, MCP registered in ~/.claude.json, CLAUDE.md contains the managed block. Deployed-hook byte-match integrity + hook-target-exists checks run for BOTH harnesses; deploy_hook_scripts writes via temp file + atomic replace.
4. Dedupe recall shims: one shared shim source in integrations/common/hooks/, deployed by both installers under their harness-local filenames; delete the two near-identical copies.
5. session_ending_check.py: relocate source into the entity tree (~/.narada/.smriti/hooks/) — which is itself a versioned, backup-pushed git repo, so this IS the reproducible home; it is Suti-specific and does not belong in the entity-agnostic smriti repo. Repoint settings.json only after the destination exists and imports cleanly; keep the old ~/.claude/hooks copy until verified; doctor gains a generic hook-target-exists check; INSTALL.md documents entity-repo-clone-before-harness-install ordering.
6. Tests for 1-4 (managed-block merge idempotence + preservation, PS command generation/decoding, doctor semantic checks both harnesses, shared-shim deploy).

Track B — cross-agent review loop (the dogfood):
1. Suti installs the official plugin in Claude Code: /plugin marketplace add openai/codex-plugin-cc, /plugin install codex@openai-codex, /reload-plugins, /codex:setup. Verify subscription auth.
2. Run /codex:adversarial-review against this spec file. Claude explicitly accepts/rejects each finding in the spec's Open questions section; revise once.
3. Claude implements Track A on a branch.
4. Run /codex:review --base master on the diff. Author fixes substantiated findings only; one recheck round.
5. Both agents' review verdicts get written through smriti (branch projects/smriti) so review state is shared memory, not chat-local — the piece no off-the-shelf orchestrator provides.
6. Defer Agent Orchestrator until we genuinely want parallel worktrees + live monitoring; skip AWS CAO (WSL/tmux cost not justified). Note: cross-harness session-log reading (this audit read Codex's rollout JSONLs directly) already covers "monitor the other harness" for after-the-fact review at zero install cost.

## Open questions

_None yet._

## Adversarial review — Codex pass 1 (2026-07-16, verdict: needs-attention)

Findings and Claude's dispositions. Revisions folded into the plan below.

1. **[high] Marker migration duplicates every existing generated agent doc — ACCEPTED.**
   Correct: the live ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md contain
   previously-generated smriti content with no markers; append-if-no-markers
   would leave the stale copy active and grow AGENTS.md toward Codex's 32 KiB
   cap. Amendment: installers are **fail-closed** on unmarked or malformed
   files (no markers / duplicate marker pairs / reversed markers → refuse to
   write, print migration instruction, exit cleanly). A separate one-time
   `install --migrate-agent-doc` backs up the file, replaces the legacy
   generated sections (recognized by their exact heading set, since the
   local file has diverged from any generated text verbatim) with one marked
   block, and preserves all other content byte-for-byte.
2. **[high] "Invokes wake.py" too weak to classify a hook as working — ACCEPTED.**
   Correct: constraint as written contradicted the doctor's semantic
   requirements (a hook can target wake.py yet lack SMRITI_WAKE, use the
   wrong framing, or miss matchers). Amendment: one canonical hook model in
   integrations/common, used by BOTH installer and doctor: decoded command
   (incl. PowerShell -EncodedCommand), required env (SMRITI_WAKE on,
   SMRITI_ROOT → entity tree, harness-correct framing + audience),
   normalized wake.py path, required event matchers (Codex:
   startup|resume|clear|compact). Installer: semantically equivalent →
   untouched; targets wake.py but deficient → upgrade in place with config
   backup; unrelated hooks never modified.
3. **[high] session_ending_check.py moved outside the reproducible deploy path — PARTIALLY ACCEPTED.**
   The premise misses that ~/.narada is itself a versioned git repository,
   snapshotted and pushed by backup.py on every wake and session end —
   relocating the file there puts it *under* version control, and
   fresh-machine restore is "clone the entity repo", which precedes harness
   install in every recovery path. Remedy of keeping it in the smriti repo
   REJECTED (Suti-specific content in an entity-agnostic repo). Hardening
   portions ACCEPTED: settings.json is only repointed after the destination
   file exists and imports cleanly; the old ~/.claude/hooks copy is kept
   until verified; doctor gains a generic check that every hook command in
   settings.json references an existing file (catches silent-missing for
   all hooks, not just this one); INSTALL.md documents entity-repo-clone-
   before-harness-install ordering.
4. **[medium] Doctor leaves Codex copy-deployment drift invisible — ACCEPTED.**
   Correct and cheap: the byte-match integrity check applies to both
   harnesses' deployed hooks, deploy_hook_scripts writes via temp file +
   atomic replace, and doctor reports unreadable or mismatched copies.

## Adversarial review — Codex pass 2 on the revised plan (2026-07-17, final round)

Amendments 1, 2, 4 judged adequate; finding-3 rebuttal accepted given the
entity-tree-is-versioned context ("no further objection"). One new finding:

5. **[high] Fail-closed agent-doc refusal reported as successful install — ACCEPTED.**
   Correct: pass-1 amendment had the installer exit 0 on refusal (a reflex
   from the hook never-break contract, wrong for an installer). Both live
   agent docs are unmarked today, so every upgrade hits this path, and the
   dispatcher would print success after a partial install. Amendment:
   agent-doc validation runs as a PREFLIGHT before any harness mutation;
   migration-required or malformed-marker refusal propagates as a nonzero /
   explicitly-failed result through the dispatcher; no success message, and
   no hook/config mutations are applied first.

## Tests / acceptance criteria

- Agent-doc managed block: rewrite-in-place idempotent; content outside the
  block preserved byte-for-byte; unmarked file → preflight refusal BEFORE any
  harness mutation, nonzero/failed dispatcher result, no success message, no
  hook/config changes applied, file untouched; duplicate/reversed markers →
  refuse without writing;
  --migrate-agent-doc on a copy of the real legacy CLAUDE.md yields marked
  block + preserved personal sections + backup file; interrupted write never
  leaves a partial doc (temp + atomic replace).
- Canonical hook model: PS -EncodedCommand decodes and classifies as
  equivalent; sh-form classifies as equivalent; wake.py-targeting hook with
  missing env/matcher classifies as deficient → installer upgrades with
  backup; unrelated hook untouched; doctor and installer share the classifier
  (single source, no drift).
- Doctor: false-FAIL cases from the 2026-07-16 audit now PASS against the
  live config; MCP superset match ignores tools.* subtables; deployed-hook
  byte-match runs for BOTH harnesses; missing hook-target file → FAIL with
  path; --harness claude-code suite covers wake wiring, hooks, MCP,
  managed block.
- Shared recall shim: one source file; both installers deploy it; deployed
  bytes match; the two legacy per-harness shims are gone.
- Full suite green (baseline: 215 passed).

## Implementation notes

_Filled during build._

## Findings

_Filled as the build produces lessons._

## Documentation

_Once shipped, this section becomes the canonical doc._

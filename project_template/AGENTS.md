# AGENTS.md

<!-- Project-level AGENTS.md for Codex CLI. The user-global memory contract
     lives in ~/.codex/AGENTS.md (deployed by `python scripts/install.py
     --harness codex`). This file is for project-specific orientation and
     should stay in sync with CLAUDE.md alongside it. -->

## Project
<!-- PROJECT_NAME — one-line description -->

## Commands

```bash
# TODO: fill in once tech stack is established
```

## Structure

```
.ai/               — project knowledge
  todo.md          — project roadmap and tasks
  knowledge/       — reference docs (spec, architecture, glossary, conventions)
```

## First session on a new project

Before writing any code, orient yourself. Then ask:

1. **What is this project?** — one paragraph, what it does and why it exists.
2. **Who is it for?** — users, audience, context.
3. **What's the tech stack?** — language, framework, database, deployment.
4. **What exists already?** — is there code? a prototype? starting from scratch?
5. **What's the first milestone?** — what does "working" look like?

Fill in: this file's project description and commands, `.ai/knowledge/spec.md`,
`.ai/knowledge/architecture.md`, `.ai/knowledge/glossary.md`, `.ai/todo.md`.

## Reference — read when the work needs it

These are textbooks. Look things up, don't pre-load.
- .ai/knowledge/spec.md
- .ai/knowledge/architecture.md
- .ai/knowledge/glossary.md
- .ai/knowledge/conventions.md

## Memory

Memory persistence goes through smriti. The full memory contract (when to
write, when to call `smriti_read`, branch conventions) lives in the
user-global `~/.codex/AGENTS.md` — Codex concatenates that with this file
on every run. This project-level file only carries project-specific
overrides.

Use `smriti_write(content, branch)` for session observations, decisions,
and project notes. Branch suggestions:
- `projects/{project-name}` for project-specific notes
- `journal` for significant moments
- `notes` for general observations

Identity briefing is force-loaded by the SessionStart hook in
`~/.codex/config.toml` — it appears as developer context before the first
turn.

## Rules

- Check .ai/knowledge/conventions.md before introducing new patterns
- Keep commits atomic — one logical change per commit

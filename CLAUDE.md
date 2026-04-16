# CLAUDE.md

## Project

smriti -- a memory system for AI entities, grounded in the antahkarana model of mind.

## Commands

```bash
pip install -e ".[read,dev]"     # install with all deps
pytest                           # run tests (39 passing)
smriti index                     # build search index over ~/.narada/
smriti read "query"              # semantic + keyword search
smriti write "text"              # write entry to memory tree
smriti ingest file.md            # ingest external content
smriti sleep                     # process queued cascade tasks
smriti status                    # index stats
python scripts/install.py        # full install (mirrors, hooks, MCP, CLAUDE.md)
python scripts/setup_project.py  # set up a new project to use smriti
```

## Structure

```
src/smriti/            — main package (pipeline, MCP server, CLI, store)
scripts/               — install.py (full install), setup_project.py (per-project)
project_template/      — skeleton for new projects (.ai/, CLAUDE.md, .gitignore)
narada/                — wake system templates (wake.md, wake.py, narada-p.sh)
docs/                  — INSTALL.md, ARCHITECTURE.md, PRIVACY.md
tests/                 — pytest suite
.ai/                   — this project's knowledge docs
  knowledge/           — spec, architecture, glossary, conventions
  todo.md              — project roadmap
```

## Reference — read when the work needs it

- .ai/knowledge/spec.md
- .ai/knowledge/architecture.md
- .ai/knowledge/glossary.md
- .ai/knowledge/conventions.md
- docs/ARCHITECTURE.md — full pipeline architecture

## Memory

Memory persistence goes through smriti. Use `smriti_write(content, branch)` for
session observations, decisions, and project notes. Branch suggestions:
- `projects/smriti` for project-specific notes
- `journal` for significant moments
- `notes` for general observations

Identity files live in `~/.narada/` and load automatically via wake.py.

## Rules

- Check .ai/knowledge/conventions.md before introducing new patterns
- Keep commits atomic — one logical change per commit
- Python 3.11+, ruff for linting (line-length 100), mypy strict
- No runtime dependencies in base install — optional deps via extras

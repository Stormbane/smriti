# AGENTS.md

Orientation for an agent working on this repo. Codex loads this file; Claude Code loads [`CLAUDE.md`](CLAUDE.md), which says the same thing. Keep the two identical apart from this line. Status as of 2026-11-06.

## What smriti is

smriti gives one AI agent persistent memory and a continuous self across sessions and harnesses (Claude Code, Codex).
The memory is a tree of plain markdown files on local disk (`$SMRITI_ROOT`, default `~/.smriti`, or `~/.narada` where that already exists), searched through a local SQLite index; the nightly sends each day's log to an LLM CLI, and backups push to a git remote you choose.
It grew inside one setup (the entity Narada and its user Suti, the maintainer) and is being generalised: alpha, Windows-first, one maintainer; CI runs lint, strict types and the tests on Windows, Linux and macOS.

## Read this first

1. [`.ai/features/smriti-renewal.md`](.ai/features/smriti-renewal.md): the plan, and the most accurate status ("Where things stand", "Progress", Phases 0-7).
2. [`README.md`](README.md): what runs today, how an agent should use it, install.
3. [`docs/SELF.md`](docs/SELF.md): the wake briefing, the self files, how the self changes.
4. [`.ai/features/nightly-cycle.md`](.ai/features/nightly-cycle.md): the contract for capture, the nightly and the morning message.

Historical: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), `.ai/knowledge/spec.md`, `.ai/knowledge/architecture.md` and `.ai/knowledge/glossary.md` describe the April 2026 design. Of what was built from it, only search, writes and the `index.md` upkeep remain. Read them for intent, not for status.

## Status

**Live** (runs daily in the reference install):
- `smriti write`, `smriti read`, and the MCP server (`python -m smriti.mcp_server`): dated markdown writes (which also regenerate the parent directories' `index.md` listings), and hybrid search (0.5 vector + 0.3 keyword + 0.2 trunk distance; the CLI also reranks, the MCP path doesn't).
- Day-log capture: `smriti logd` merges Claude Code transcripts, Codex rollouts and voice transcripts into `log/YYYY/MM/DD.jsonl` (`src/smriti/daylog/`). It is off until `<root>/.smriti/daylog.json` says `"capture": true` or lists `"sources"` (hardening H11.1).
- `smriti nightly` (03:00): reconcile → render → digest (one `claude -p` call per day, `codex exec` as fallback) → week/month/year rollups → index → witness (a git commit plus an OpenTimestamps stamp) → status file.
- `smriti morning` (07:00): one message a day composed from the digest, from the configured agent to the configured user; a profile can supply its own prompt (the reference profile's carries its persona).
- The wake briefing: a SessionStart hook prints `smriti.wake.briefing()`, at most 9,500 characters.
- The embedding service (hardening H10): `smriti embed serve`, one per memory root, holds the model; the MCP servers and `smriti read` ask it over authenticated loopback TCP and start it on demand, and it exits after 15 idle minutes. `smriti index` (and so the nightly) loads its own model. A write without vectors is indexed for keyword search, its vectors pending until the service fills them. `SMRITI_EMBED_SERVICE=0` turns it off. `SECURITY.md` has its trust boundary.
- Backups: `<root>/.smriti/backup.py` commits the memory tree hourly (the `smriti-backup` task; it pushes only when there is something new) and in the nightly. Sessions don't trigger it.

**Removed** (2026-11-05, hardening H3; the code is in the git history): the deep pipeline that `smriti sleep --deep` ran (cognitive cascade, JUDGE, ingest and routing, consolidation, thread synthesis, actionables, journal rollups, the wake-context rebuild), its task queue and file watcher, and the `sleep`, `queue`, `daemon`, `watch`, `ingest` and `merge-concepts` commands. `smriti eval` keeps only its search cases.

**Legacy:**
- qmd as a recall backend (`smriti recall index`, `smriti recall daemon`); smriti's own index is the default.
- The Hermes harness (`--harness hermes`).
- The PreCompact staging hook.
- The PostToolUse ambient-recall hook: `scripts/install.py` still installs it, but it is off in the reference install and being retired (plan 2.4).

**Roles and profiles** (hardening H5):
- The day-log's speakers are roles, `user` and `agent`; `user_name` and `agent_name` in `<root>/.smriti/daylog.json` (default "User", "Agent") name them in the render, the prompts and the core tools. Old logs' `"suti"`/`"narada"` are read as the roles, with turn ids unchanged.
- The reference installation's specifics are the optional profile `reference`, its own package outside smriti's wheel (`profiles/reference/`, the distribution `smriti-reference`, found through the `smriti.profiles` entry-point group; hardening H11.6), enabled by `"profile": "reference"` in the same file: the MCP tools `tell_suti`, `smriti_ask_suti`, `smriti_propose_goal_shift` and `smriti_resolve_outbox`, the wake's `HANDS`, `SUTI NOW` and `ACROSS SURFACES` sections, and the morning persona. Without a profile, none of these exist, and smriti's own package carries no profile code.
- The day boundary follows the system's time zone (DST included); `SMRITI_DAY_UTC_OFFSET` or daylog.json's `timezone` overrides it.
- The root is `SMRITI_ROOT` (or the legacy `NARADA_ROOT`); else `~/.narada` where it is a directory (the default before 0.2, so an existing install keeps its tree, even beside a `~/.smriti`); else `~/.smriti`. `smriti status` names the rule that chose it.

## Facts

<!-- doc-facts:begin (generated by scripts/doc_facts.py; do not edit by hand) -->
- **CLI:** 15 `smriti` subcommands: `backup`, `doctor`, `embed`, `eval`, `index`, `logd`, `metrics`, `morning`, `nightly`, `read`, `recall`, `status`, `tasks`, `witness`, `write`. Nested: `embed` has `serve`, `status`, `stop`; `recall` has `query`, `status`, `stats`, `index`, `daemon`.
- **MCP:** 9 core tools: `smriti_read`, `smriti_write`, `smriti_status`, `smriti_write_handoff`, `smriti_add_todo`, `smriti_record_finding`, `smriti_record_decision`, `smriti_record_feature`, `smriti_update_feature_status`.
- **MCP, `reference` profile:** 4 more tools when it is enabled: `tell_suti`, `smriti_ask_suti`, `smriti_propose_goal_shift`, `smriti_resolve_outbox`.
- **Tests:** 670 test functions in 57 files under `tests/` (pytest collects more, because some are parametrised).
<!-- doc-facts:end -->

Regenerate with `python scripts/doc_facts.py --write`; `tests/test_doc_facts.py` fails when this block drifts from the code.

## Layout

```
src/smriti/
  cli.py                 the `smriti` command
  mcp/                   MCP server over stdio, no dependencies: protocol, tool table, tools by group
  store/                 writer, index pages (index.md upkeep), indexer, index policy, schema, search
  embed/                 embedding identity; the embedding service, its client, protocol and runtime dir
  daylog/                capture adapters, logd, render, digest, rollups, nightly, morning, schedule
  wake/                  briefing.py: the wake briefing, with a hook for a profile's sections
  profiles/              the profile seam: installed profiles, found by entry point (tools, wake sections, morning prompt)
  witness.py, backup.py  tamper-evidence; the backup trigger
  integrations/          claude_code, codex, hermes installers; shared hooks; the managed agent-doc block
  llm/                   providers: claude_cli, codex_cli, anthropic_api, openai_api, ollama
  recall/                ambient-recall hook backends (smriti, qmd)
  private/               encrypted private store
  eval/                  search evaluation: cases, runner, synthetic fixture
  _vendored/memsearch/   vendored MIT code (see NOTICE.md)
scripts/                 install.py, setup_project.py, doc_facts.py
memory_template/         skeleton for a new memory root
narada/                  wake.py, backup.py, narada-p.sh, copied into <root>/.smriti/ by the installer
project_template/        skeleton for a project that uses smriti
tests/                   pytest; conftest.py points SMRITI_ROOT at a temp dir
profiles/reference/      the reference profile (the maintainer's installation): its own package, smriti-reference, with its tests
```

## Dev commands

```bash
pip install -e ".[read,dev]"                      # editable install: the installer reads templates from the checkout
pip install -e profiles/reference                 # the reference profile, for its own tests (the core runs without it)
python -m pytest -q --ignore=tests/test_perf.py tests profiles/reference/tests  # perf guards are marked slow and excluded by default
ruff check <files>                                # line length 100, bugbear, blind excepts
mypy <files>                                      # strict
python scripts/doc_facts.py --write               # refresh the generated counts in README.md, CLAUDE.md, AGENTS.md
smriti status                                     # index and day-log health for $SMRITI_ROOT
```

Tests import `smriti` from wherever it is installed. In a second checkout or worktree, set `PYTHONPATH=src` so they exercise that checkout.

## Rules

- Python 3.11+. `ruff` and `mypy --strict` are the standard. Ruff checks `E4`, `E7`, `E9` and `F`, plus `E501` (line length 100), `B` (bugbear) and `BLE` (blind excepts); vendored code is exempt from the last three. `ruff check src tests scripts profiles` passes, and CI enforces `mypy --strict` on the whole package and the reference profile on every OS (vendored code excepted), and checks that smriti's wheel has no reference code.
- Narrow an `except Exception` to what can be raised. Keep one only where any failure must be survived (a top-level handler, a hook, a non-critical step), with `# noqa: BLE001 — <reason>`; it still logs the failure or records it in a status the caller sees.
- No runtime dependencies in the base install (`dependencies = []`). Optional ones go in extras: `read`, `api`, `codex`, `private`, `witness`, `dev`.
- Check `.ai/knowledge/conventions.md` before introducing a new pattern.
- Atomic commits: one logical change per commit.
- The repo is public. No personal data in code, tests, fixtures or docs; use synthetic names (the agent Ari, the user Sam). Private audit reports go in `docs/audits/`, which is gitignored.
- Tests never touch the real memory tree.

## Memory (the maintainer's setup only)

If this session has the smriti MCP server, it is the maintainer's own install. Record project notes with `smriti_write(content, branch="projects/smriti")`, and call `smriti_read` before guessing about past decisions. A fresh clone has no such server; ignore this section.

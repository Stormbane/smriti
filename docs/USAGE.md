# smriti — Library Usage

This document covers:

- **Library use** — wiring smriti into a custom Python agent (later
  sections). Independent of any agent harness.
- **Codex CLI integration** — the second supported harness alongside
  Claude Code (see below).

For the Claude Code install path (SessionStart hook,
`~/.claude/CLAUDE.md`, MCP server registration), see
[INSTALL.md](INSTALL.md).

---

## Codex CLI integration

`smriti` ships a Codex adapter that wires identity into every Codex
session at the protocol level — same shape as Claude Code, different
config-file format and hook-output framing.

### What gets installed

Run::

    pip install -e '.[codex]'    # adds tomli-w for TOML writing
    python scripts/install.py --harness codex

This sets up:

- `[features] codex_hooks = true` in `~/.codex/config.toml`.
- `[[hooks.SessionStart]]` running `python -P -m smriti.wake` (the
  installed package's wake) with `SMRITI_WAKE_FRAMING=codex-json`. Codex runs this **before the
  first user turn** and appends the briefing JSON
  (`hookSpecificOutput.additionalContext`) to the developer context.
  The agent has no opportunity to skip it — identity loads at the
  protocol level, not by request.
- `[mcp_servers.smriti]` registering the same MCP server Claude Code
  uses, so `smriti_read` / `smriti_write` are first-class tools.
- `~/.codex/AGENTS.md` composed from the shared
  `smriti/templates/AGENT.md` plus a Codex-specific addendum (about
  AGENTS.md precedence rules, the 32 KiB project-doc cap, the
  SessionStart force-injection mechanism).

Re-running the install is idempotent. Existing config keys are
preserved. Before any write the file as it was goes to a new
`config.toml.smriti-<UTC time>.bak` beside it; the first of these is
never removed, and five are kept in all.

### Force-injection guarantee

The wake briefing isn't request-shaped — it's hook-shaped. Codex's
SessionStart hook fires under Codex's own process before the model
sees turn 1, and the JSON output is merged into the developer
context. Same mechanism Claude Code uses, just over Codex's hooks
spec.

References: [Codex hooks docs](https://developers.openai.com/codex/hooks),
[Codex config reference](https://developers.openai.com/codex/config-reference).

### Skip flags

    python scripts/install.py --harness codex --skip-config
    # writes AGENTS.md only, leaves config.toml untouched

Useful if you maintain your `config.toml` by hand and only want
smriti's AGENTS.md drop-in.

---

## Synthetic evaluation

Install `smriti[read]`, then run `smriti eval`.
Each run copies the bundled fictional Ari/Sam memory tree into a fresh temporary
folder, builds its own index, and keeps its metrics there. The folder is removed
on exit. No existing memory tree or index is used by default. Embedding models use
the normal model cache and may need an initial download.

The corpus has 20 short files and 14 search cases, including dated decisions,
paraphrases, a superseded fact (the newest source must rank first), and a topic
with no answer (any returned result fails that case). Scores measure retrieval;
these difficult cases may fail with the current search implementation.

Use `smriti eval --baseline ./eval-baseline.json` to keep a JSON report. Bare
`--baseline` writes only inside the disposable folder; it is removed on exit.
There is no CLI custom-tree option. Python callers may pass `db_path` to
`run_search_cases` for searches of a disposable, read-only snapshot of an explicit
index (stop its writer while the database and WAL are copied). The library sandbox
temporarily scopes process-wide environment and metrics state, so run eval
separately from concurrent store operations in the same process.

---

## Library use

The harness-agnostic surface is in three packages:

- `smriti.llm` — pluggable LLM providers (Anthropic API, Claude Code
  CLI, OpenAI, Ollama).
- `smriti.store.search` — hybrid vector + FTS5 search over the memory
  tree's index (`search_with_status`, on a connection from
  `smriti.store.schema.open_readonly`).
- `smriti.wake` — the same identity + threads + project-context
  briefing that Claude Code's SessionStart hook injects, exposed as a
  pure-assembly function returning a string.
- `smriti.store.writer` — write entries that get crosslinked, indexed,
  and found by later searches.
- `smriti.templates.AGENT.md` — the harness-neutral memory contract
  shipped as package data.

---

## Reference example: `examples/python_agent.py`

The repo ships a minimal REPL agent that exercises every part of the
library surface. It is the canonical regression check — anything that
breaks library agnosticism breaks this script.

### Run it

```bash
# Default (auto-detect): claude_cli if Claude Code is on PATH, else
# anthropic_api if ANTHROPIC_API_KEY is set, else openai_api, else ollama.
python examples/python_agent.py
```

### Per-provider invocations

#### Anthropic API (with prompt caching)

```bash
ANTHROPIC_API_KEY=sk-ant-... \
    SMRITI_LLM_PROVIDER=anthropic_api \
    python examples/python_agent.py
```

Requires the `anthropic` SDK: `pip install -e '.[api]'` or
`pip install anthropic`. The system prompt is sent with
`cache_control: ephemeral`, so repeated calls within the 5-minute TTL
get a 90% discount on system input tokens. See cost rates in
`src/smriti/llm/providers/anthropic_api.py:_RATES`.

Override default models per role:

- `SMRITI_EXECUTOR_MODEL` (default `claude-sonnet-4-6-20250514`)
- `SMRITI_MODEL` (judge — default `claude-haiku-4-5-20251001`)

#### Claude Code CLI (subscription)

```bash
SMRITI_LLM_PROVIDER=claude_cli \
    python examples/python_agent.py
```

Requires the `claude` CLI on `PATH`. No API key needed; uses your
Claude Code subscription. Multi-turn rendering uses simple
`Human:`/`Assistant:` role markers (the underlying `claude -p` is
single-shot per call); a future revision can route through
`--resume <session-id>` for native context preservation.

#### OpenAI API

```bash
OPENAI_API_KEY=sk-... \
    SMRITI_LLM_PROVIDER=openai_api \
    python examples/python_agent.py
```

Requires `pip install openai`. Override models with
`SMRITI_OPENAI_EXECUTOR_MODEL` (default `gpt-4o`) and
`SMRITI_OPENAI_JUDGE_MODEL` (default `gpt-4o-mini`). For OpenAI-
compatible third-party endpoints (Together, Groq, vLLM), set
`OPENAI_BASE_URL` to their endpoint.

#### Ollama (local)

```bash
SMRITI_LLM_PROVIDER=ollama \
    SMRITI_OLLAMA_EXECUTOR_MODEL=llama3.1:8b \
    python examples/python_agent.py
```

Requires the Ollama daemon at `http://localhost:11434` (override via
`SMRITI_OLLAMA_URL`). No Python deps beyond the stdlib.
`SMRITI_OLLAMA_JUDGE_MODEL` defaults to `llama3.2:3b`.

### REPL commands

| Command                     | What it does                                             |
|-----------------------------|----------------------------------------------------------|
| `<text>`                    | Normal turn: memory search, then LLM call with history.  |
| `/write <branch> <text>`    | Persist a note via `smriti.store.writer.write_entry`.    |
| `/provider`                 | Show active provider, model, history depth.              |
| `/provider list`            | Enumerate registered providers + availability.           |
| `/reset`                    | Clear conversation history.                              |
| `exit`, `quit`, `Ctrl-D`    | End session; auto-journal turn-by-turn summary.          |

---

## Building your own agent

The library entry points used by the example:

```python
from smriti.llm import call_llm, list_providers, Message
from smriti.store.schema import open_readonly
from smriti.store.search import search_with_status
from smriti.store.writer import write_entry
from smriti.wake import briefing
```

### Composing a system prompt

```python
from pathlib import Path
import smriti as _s

agent_md = (Path(_s.__file__).parent / "templates" / "AGENT.md")
contract = agent_md.read_text(encoding="utf-8").format(memory_rel="~/.smriti")
context = briefing(memory_root=Path.home() / ".smriti")  # or briefing(): the default root
system_prompt = f"{contract}\n\n---\n\n{context}"
```

### Multi-turn calls

```python
history: list[Message] = []
history.append(Message("user", "hello"))
response = call_llm(system=system_prompt, messages=history)
history.append(Message("assistant", response.text))
```

### Searching memory

```python
from smriti.core.tree import smriti_db_path

conn = open_readonly(smriti_db_path())  # a reader never holds up a writer
try:
    outcome = search_with_status(conn, "how does Sam like answers", top_k=3)
finally:
    conn.close()
for note in outcome.notes():  # keyword-only, or nothing close to the query
    print(note)
for r in outcome.results:
    print(r.source, f"{r.score:.2f}", r.content[:200])
```

Nothing puts the results in front of the model for you: add the ones
you want to the system prompt or the next message, as the example does
on every turn.

### Picking a provider programmatically

```python
for row in list_providers():
    if row["available"]:
        print(row["name"], row["executor_model"])
```

`list_providers()` is also handy for building `/provider list`-style
introspection commands or for picking a fallback when the preferred
provider is offline.

### Writing memory

```python
from smriti.store.writer import write_entry

path = write_entry(
    "Suti decided X because Y on 2026-05-01.",
    branch="journal",
    source_hint="my_agent",
)
```

The writer dates the entry, places it under `journal/YYYY/MM/weekN/`,
indexes it, and regenerates any `index.md` above it. Anything written
here is found by the next search.

---

## Provider auto-detection

When `SMRITI_LLM_PROVIDER` is unset, `get_provider()` picks the first
available in this order:

1. `anthropic_api` if `ANTHROPIC_API_KEY` is set and the SDK imports.
2. `openai_api` if `OPENAI_API_KEY` is set and the SDK imports.
3. `claude_cli` (Claude Code subscription) — the provider's own
   `is_available()` check at call time decides whether the CLI is
   actually on PATH.

Override at any time by exporting `SMRITI_LLM_PROVIDER` to one of:
`anthropic_api` (alias `claude_api`), `claude_cli`, `openai_api`
(alias `openai`), `ollama`.

To add a new provider, drop a module under
`src/smriti/llm/providers/` conforming to the `LLMProvider` protocol,
register it in `factory._REGISTRY`, and (optionally) update
`_autodetect()`.

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
- `[[hooks.SessionStart]]` pointing at `~/.narada/.smriti/wake.py`
  with `SMRITI_WAKE_FRAMING=codex-json`. Codex runs this **before the
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
preserved; a `.toml.bak` is dropped before any write.

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

### Pending parity items

- **Ambient recall on file touches.** Claude Code wires
  `PostToolUse` on `Read|Edit|Write`; the equivalent Codex hook
  (`[[hooks.PostToolUse]]` matching `apply_patch` and friends) is not
  yet wired by the installer. For now, Codex sessions rely on
  agent-initiated recall — the "When to call `smriti_read`" section
  in `AGENTS.md` is load-bearing here.

---

## Library use

The harness-agnostic surface is in three packages:

- `smriti.llm` — pluggable LLM providers (Anthropic API, Claude Code
  CLI, OpenAI, Ollama).
- `smriti.recall` — hybrid vector + FTS5 search over the memory tree,
  plus a tool-wrapper that fires recall after a callable runs.
- `smriti.wake` — the same identity + threads + project-context
  briefing that Claude Code's SessionStart hook injects, exposed as a
  pure-assembly function returning a string.
- `smriti.store.writer` — write entries that get crosslinked, indexed,
  and surfaced by future recall.
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
| `<text>`                    | Normal turn: ambient recall, then LLM call with history. |
| `/write <branch> <text>`    | Persist a note via `smriti.store.writer.write_entry`.    |
| `/tool read <path>`         | Run `read_file` wrapped with `smriti.recall.wrap_tool`.  |
| `/tool ls <path>`           | Run `list_dir` wrapped with `smriti.recall.wrap_tool`.   |
| `/provider`                 | Show active provider, model, history depth.              |
| `/provider list`            | Enumerate registered providers + availability.           |
| `/reset`                    | Clear conversation history.                              |
| `exit`, `quit`, `Ctrl-D`    | End session; auto-journal turn-by-turn summary.          |

---

## Building your own agent

The four library entry points used by the example:

```python
from smriti.llm import call_llm, list_providers, Message
from smriti.recall import run_recall, wrap_tool
from smriti.store.writer import write_entry
from smriti.wake import briefing
```

### Composing a system prompt

```python
from pathlib import Path
import smriti as _s

agent_md = (Path(_s.__file__).parent / "templates" / "AGENT.md")
contract = agent_md.read_text(encoding="utf-8").format(memory_rel="~/.narada")
context = briefing(memory_root=Path.home() / ".narada")
system_prompt = f"{contract}\n\n---\n\n{context}"
```

### Multi-turn calls

```python
history: list[Message] = []
history.append(Message("user", "hello"))
response = call_llm(system=system_prompt, messages=history)
history.append(Message("assistant", response.text))
```

### Wrapping a tool with ambient recall

```python
def my_read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")

wrapped = wrap_tool(my_read, query_from=lambda a, kw, out: a[0])
result = wrapped("notes.md")
print(result.output)         # the file contents
print(result.recall_block)   # injectable system-reminder text (or "")
```

`result.recall_block` is the same `<system-reminder>...</system-reminder>`
shape Claude Code's PostToolUse hook emits — feed it back to the LLM as
the next user message (or as a tool-result annotation in tool-calling
APIs) to give the agent ambient memory after every file touch.

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
and queues it for crosslink + index. Anything written here is
searchable on the next `run_recall()` call.

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

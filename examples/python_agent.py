"""examples/python_agent.py — minimal harness-agnostic agent.

Demonstrates smriti as a library, not as a Claude Code harness:

    - LLM calls go through ``smriti.llm.call_llm``, so the same script
      runs against Anthropic, OpenAI, claude -p, or local Ollama by
      flipping ``SMRITI_LLM_PROVIDER``.
    - Memory search on every user prompt via
      ``smriti.store.search.search_with_status``, the hybrid search the MCP
      server's ``smriti_read`` runs; the best matches join the system prompt.
    - Writes go through ``smriti.store.writer.write_entry`` so anything
      the agent journals is immediately searchable on the next turn.
    - System prompt is composed from the harness-neutral AGENT.md
      contract (in the package) plus the live wake briefing assembled
      by ``smriti.wake.briefing`` — the same identity + threads +
      project context that Claude Code's SessionStart hook injects.

Run::

    # Default (auto-detect): uses claude_cli if Claude Code is on PATH,
    # else anthropic_api if ANTHROPIC_API_KEY is set, else openai_api,
    # else ollama (if its daemon is up).
    python examples/python_agent.py

    # Explicit provider:
    SMRITI_LLM_PROVIDER=ollama SMRITI_OLLAMA_EXECUTOR_MODEL=llama3.1:8b \\
        python examples/python_agent.py

    OPENAI_API_KEY=... SMRITI_LLM_PROVIDER=openai_api \\
        python examples/python_agent.py

REPL:
    you> <text>           normal turn (memory search + LLM, history threaded)
    /write <branch> <text> persist a note (e.g. /write journal Today I ...)
    /provider             show which provider/model is active
    /provider list        list all known providers + availability
    /reset                clear conversation history
    exit | quit           end session (auto-journals turn history)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from smriti.core.tree import smriti_db_path
from smriti.llm import Message, call_llm, get_provider, list_providers
from smriti.store.schema import open_readonly
from smriti.store.search import SearchResult, search_with_status
from smriti.store.writer import write_entry
from smriti.wake import briefing

# AGENT.md ships inside the package — same file the Claude Code adapter
# wraps into ~/.claude/CLAUDE.md.
import smriti as _smriti
AGENT_MD = Path(_smriti.__file__).parent / "templates" / "AGENT.md"


def _build_system_prompt(memory_root: Path) -> str:
    """Compose AGENT.md contract + live wake briefing.

    Mirrors what Claude Code does: drop the memory contract via
    ~/.claude/CLAUDE.md, then load the wake briefing on SessionStart.
    Here we concatenate both into a single system prompt because
    plain LLM APIs have no SessionStart equivalent.
    """
    memory_rel = (
        f"~/{memory_root.relative_to(Path.home()).as_posix()}"
        if memory_root.is_relative_to(Path.home())
        else str(memory_root)
    )
    contract = AGENT_MD.read_text(encoding="utf-8").format(memory_rel=memory_rel)
    wake = briefing(memory_root=memory_root)
    return f"{contract}\n\n---\n\n{wake}"


def _search(query: str, top_k: int = 3) -> list[SearchResult]:
    """The best matches for *query* in the memory tree's index (none without one)."""
    db_path = smriti_db_path()
    if not db_path.exists():
        return []
    conn = open_readonly(db_path)  # a reader never holds up a writer
    try:
        outcome = search_with_status(conn, query, top_k=top_k, use_reranker=False)
    finally:
        conn.close()
    for note in outcome.notes():  # keyword-only, or nothing close to the query
        print(f"  [{note}]")
    return outcome.results


def _format_matches(matches: list[SearchResult]) -> str:
    if not matches:
        return ""
    lines = ["Memory (top matches from smriti's search):"]
    for m in matches:
        snippet = m.content.replace("\n", " ").strip()[:280]
        lines.append(f"- {m.source} (score {m.score:.2f}): {snippet}")
    return "\n".join(lines)


def _handle_write(rest: str) -> None:
    parts = rest.split(maxsplit=1)
    if len(parts) < 2:
        print("usage: /write <branch> <text>")
        return
    branch, content = parts
    path = write_entry(content, branch=branch, source_hint="python_agent")
    print(f"  wrote {path}")


def _auto_journal(history: list[Message], provider_name: str) -> None:
    """Write a turn-by-turn session summary to the journal branch."""
    if not history:
        return
    lines = [
        f"# Session via examples/python_agent.py ({provider_name})",
        "",
        f"{len(history) // 2} turn(s).",
        "",
    ]
    for m in history:
        tag = "**you**" if m.role == "user" else "**agent**"
        snippet = m.content.strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rstrip() + " …"
        lines.append(f"{tag}: {snippet}")
        lines.append("")
    try:
        path = write_entry(
            "\n".join(lines), branch="journal", source_hint="python_agent_example"
        )
        print(f"  auto-journaled: {path}")
    except Exception as exc:
        print(f"  auto-journal failed: {exc}")


def main() -> int:
    p = get_provider()
    memory_root = Path(os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada")))
    system_prompt = _build_system_prompt(memory_root)
    history: list[Message] = []
    print(f"smriti python agent — provider: {p.name}, "
          f"model: {p.default_model('executor') or '(provider-default)'}")
    print(f"  memory_root: {memory_root}  ({len(system_prompt)} char system prompt)")
    print("type 'exit' to quit, '/write <branch> <text>' to journal, "
          "'/provider' to show config, '/reset' to clear history.\n")

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _auto_journal(history, p.name)
            return 0
        if not user:
            continue
        if user in ("exit", "quit"):
            _auto_journal(history, p.name)
            return 0
        if user == "/provider":
            print(f"  provider={p.name} model={p.default_model('executor')!r} "
                  f"history={len(history)} turns")
            continue
        if user == "/provider list":
            for row in list_providers():
                mark = "[X]" if row["available"] else "[ ]"
                aliases = (
                    f" (aliases: {', '.join(row['aliases'])})"
                    if row["aliases"] else ""
                )
                print(
                    f"  {mark} {row['name']}{aliases}\n"
                    f"        executor={row['executor_model'] or '(provider-default)'} "
                    f"judge={row['judge_model'] or '(provider-default)'}"
                )
                if "error" in row:
                    print(f"        error: {row['error']}")
            print(f"  active: {p.name}")
            continue
        if user == "/reset":
            history.clear()
            print("  history cleared")
            continue
        if user.startswith("/write "):
            _handle_write(user[len("/write "):])
            continue

        t0 = time.monotonic()
        matches = _search(user)
        search_ms = int((time.monotonic() - t0) * 1000)

        system = system_prompt
        block = _format_matches(matches)
        if block:
            system = f"{system}\n\n{block}"

        # Thread history: prior turns + current user message.
        turn_messages = list(history) + [Message(role="user", content=user)]

        try:
            t1 = time.monotonic()
            response = call_llm(system=system, messages=turn_messages, role="executor")
            llm_ms = int((time.monotonic() - t1) * 1000)
        except Exception as exc:
            print(f"  LLM error: {exc}")
            continue

        history.append(Message(role="user", content=user))
        history.append(Message(role="assistant", content=response.text))

        print(f"\nagent> {response.text}\n")
        print(
            f"  [search {search_ms}ms · {len(matches)} matches]"
            f"  [LLM {llm_ms}ms · {response.tokens_in}+{response.tokens_out}t"
            + (f" · ${response.cost_usd:.4f}" if response.cost_usd else "")
            + f" · {len(history)//2} turns]\n"
        )


if __name__ == "__main__":
    sys.exit(main())

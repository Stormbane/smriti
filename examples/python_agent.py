"""examples/python_agent.py — minimal harness-agnostic agent.

Demonstrates smriti as a library, not as a Claude Code harness:

    - LLM calls go through ``smriti.llm.call_llm``, so the same script
      runs against Anthropic, OpenAI, claude -p, or local Ollama by
      flipping ``SMRITI_LLM_PROVIDER``.
    - Ambient recall on every user prompt via ``smriti.recall.run_recall``
      — same engine the Claude Code hook uses, just call-driven instead
      of event-driven.
    - Writes go through ``smriti.store.writer.write_entry`` so anything
      the agent journals is immediately searchable on the next turn.
    - System prompt is composed from the harness-neutral AGENT.md
      contract (in the package) plus the live wake briefing assembled
      by ``smriti.wake.briefing`` — the same identity + threads +
      project context that Claude Code's SessionStart hook injects.
    - Tool wrapping via ``smriti.recall.wrap_tool``: ``/tool read <path>``
      and ``/tool ls <path>`` show how recall fires after a tool call
      (the same pattern Claude Code's PostToolUse hook implements).

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
    you> <text>           normal turn (recall + LLM, history threaded)
    /write <branch> <text> persist a note (e.g. /write journal Today I ...)
    /tool read <path>     run read_file with recall annotation
    /tool ls <path>       run list_dir with recall annotation
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

from smriti.llm import Message, call_llm, get_provider, list_providers
from smriti.recall import run_recall, wrap_tool
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


def _format_recall(matches) -> str:
    if not matches:
        return ""
    lines = ["Ambient memory (top matches from recall):"]
    for m in matches:
        snippet = m.snippet.replace("\n", " ").strip()[:280]
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


# --- Tool definitions wrapped with smriti.recall.wrap_tool. --------------
# Each underlying tool returns a string; the wrapper bundles the output
# with a recall annotation derived from the path argument.

def _read_file(path: str) -> str:
    """Read a UTF-8 text file and return its contents (truncated to 2KB)."""
    return Path(path).read_text(encoding="utf-8", errors="replace")[:2048]


def _list_dir(path: str) -> str:
    """List directory entries one per line."""
    return "\n".join(sorted(p.name for p in Path(path).iterdir()))


# query_from receives (args, kwargs, output) — we use the path arg.
read_tool = wrap_tool(
    _read_file,
    query_from=lambda a, kw, out: a[0] if a else kw.get("path"),
    name="read_file",
)
ls_tool = wrap_tool(
    _list_dir,
    query_from=lambda a, kw, out: a[0] if a else kw.get("path"),
    name="list_dir",
)


def _handle_tool(rest: str) -> str:
    """Dispatch /tool <name> <arg> and return any recall block to thread."""
    parts = rest.split(maxsplit=1)
    if len(parts) < 2:
        print("usage: /tool {read|ls} <path>")
        return ""
    cmd, arg = parts
    try:
        if cmd == "read":
            result = read_tool(arg)
        elif cmd == "ls":
            result = ls_tool(arg)
        else:
            print(f"unknown tool {cmd!r}; try 'read' or 'ls'")
            return ""
    except Exception as exc:
        print(f"  tool error: {exc}")
        return ""

    print(f"--- {cmd} {arg} ---")
    print(result.output)
    if result.recall_block:
        print()
        print(result.recall_block)
    return result.recall_block


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
        if user.startswith("/tool "):
            block = _handle_tool(user[len("/tool "):])
            # Thread the tool result + recall annotation into history so
            # the next agent turn sees the output the way Claude Code's
            # PostToolUse hook injects ambient memory.
            if block:
                history.append(Message(role="user", content=block))
            continue

        t0 = time.monotonic()
        recall = run_recall(user)
        recall_ms = int((time.monotonic() - t0) * 1000)

        system = system_prompt
        block = _format_recall(recall.matches)
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
            f"  [recall {recall_ms}ms · {len(recall.matches)} matches · "
            f"backend {recall.backend}]"
            f"  [LLM {llm_ms}ms · {response.tokens_in}+{response.tokens_out}t"
            + (f" · ${response.cost_usd:.4f}" if response.cost_usd else "")
            + f" · {len(history)//2} turns]\n"
        )


if __name__ == "__main__":
    sys.exit(main())

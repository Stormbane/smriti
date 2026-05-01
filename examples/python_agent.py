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
    you> <text>           normal turn (recall + LLM)
    /write <branch> <text> persist a note (e.g. /write journal Today I ...)
    /provider             show which provider/model is active
    exit | quit           end session
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from smriti.llm import call_llm, get_provider
from smriti.recall import run_recall
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


def main() -> int:
    p = get_provider()
    memory_root = Path(os.environ.get("SMRITI_ROOT", str(Path.home() / ".narada")))
    system_prompt = _build_system_prompt(memory_root)
    print(f"smriti python agent — provider: {p.name}, "
          f"model: {p.default_model('executor') or '(provider-default)'}")
    print(f"  memory_root: {memory_root}  ({len(system_prompt)} char system prompt)")
    print("type 'exit' to quit, '/write <branch> <text>' to journal, "
          "'/provider' to show config.\n")

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue
        if user in ("exit", "quit"):
            return 0
        if user == "/provider":
            print(f"  provider={p.name} model={p.default_model('executor')!r}")
            continue
        if user.startswith("/write "):
            _handle_write(user[len("/write "):])
            continue

        t0 = time.monotonic()
        recall = run_recall(user)
        recall_ms = int((time.monotonic() - t0) * 1000)

        system = system_prompt
        block = _format_recall(recall.matches)
        if block:
            system = f"{system}\n\n{block}"

        try:
            t1 = time.monotonic()
            response = call_llm(system=system, user=user, role="executor")
            llm_ms = int((time.monotonic() - t1) * 1000)
        except Exception as exc:
            print(f"  LLM error: {exc}")
            continue

        print(f"\nagent> {response.text}\n")
        print(
            f"  [recall {recall_ms}ms · {len(recall.matches)} matches · "
            f"backend {recall.backend}]"
            f"  [LLM {llm_ms}ms · {response.tokens_in}+{response.tokens_out}t"
            + (f" · ${response.cost_usd:.4f}" if response.cost_usd else "")
            + "]\n"
        )


if __name__ == "__main__":
    sys.exit(main())

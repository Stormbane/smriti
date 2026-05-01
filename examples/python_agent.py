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

import sys
import time

from smriti.llm import call_llm, get_provider
from smriti.recall import run_recall
from smriti.store.writer import write_entry


SYSTEM_PROMPT = (
    "You are an agent with persistent local memory via smriti. Each user "
    "turn is preceded by ambient recall — markdown chunks from the user's "
    "memory tree that an embedding model judged relevant. Use them to "
    "ground your reply. Be honest when recall produced nothing relevant; "
    "do not invent context. When the user shares something worth "
    "remembering, suggest they /write it."
)


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
    print(f"smriti python agent — provider: {p.name}, "
          f"model: {p.default_model('executor') or '(provider-default)'}")
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

        system = SYSTEM_PROMPT
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

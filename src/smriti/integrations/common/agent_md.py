"""Compose harness-specific agent docs from the shared AGENT.md template.

Every harness gets a ``CLAUDE.md`` / ``AGENTS.md`` style file dropped
in its config directory carrying the memory-system contract. The body
is the same generic AGENT.md ``smriti.templates.AGENT.md``); only the
top-level header line and a small harness-specific addendum differ.

Centralising the composition means edits to AGENT.md immediately flow
to every harness's installed doc on the next ``install`` run.
"""

from __future__ import annotations

from pathlib import Path

# Generic agent contract — packaged data, ships with pip install.
# Resolves to ``src/smriti/templates/AGENT.md``.
AGENT_MD_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "AGENT.md"
)


def compose_agent_doc(
    *,
    addendum: str,
    memory_rel: str,
    header: str = "# CLAUDE.md (user-global)",
) -> str:
    """Stitch the generic contract with a harness-specific addendum.

    Parameters
    ----------
    addendum:
        Harness-specific Markdown appended below the contract. Mention
        the harness's hook mechanism, config file paths, and any
        idiosyncrasies (file size caps, file precedence rules).
    memory_rel:
        ``~/.narada``-style path the agent should reference for the
        memory tree. Substituted into the AGENT.md ``{memory_rel}``
        placeholder.
    header:
        Top-level Markdown header replacing AGENT.md's title +
        documentation preamble (which is about the *template*, not
        the agent contract).

    Notes
    -----
    AGENT.md uses ``{memory_rel}`` (single brace) for substitution and
    ``{{name}}`` / ``{{project}}`` (escaped braces) for literal braces.
    ``str.format`` resolves both correctly, so callers don't need to
    pre-process the template.
    """
    body = AGENT_MD_PATH.read_text(encoding="utf-8")

    # Drop the AGENT.md preamble (title + intro paragraph). The first
    # H2 marks where the actual contract begins.
    marker = "\n## "
    idx = body.find(marker)
    if idx > 0:
        body = body[idx + 1 :]

    composed = (
        f"{header}\n\n"
        + body.rstrip()
        + "\n\n"
        + addendum
    )
    return composed.format(memory_rel=memory_rel)


__all__ = ["AGENT_MD_PATH", "compose_agent_doc"]
